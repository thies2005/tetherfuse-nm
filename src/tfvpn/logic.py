"""Pure connection state machine for the TetherFuse VPN plugin.

No GLib/D-Bus here — the D-Bus layer (plugin.py) injects three things:

* ``emit(signal, body)`` where body maps arg-name -> (variant-type, value);
* ``schedule(delay_s, fn)`` one-shot delayed callback (GLib.timeout_add);
* a backend factory + firewall instance.

State and signal semantics follow the NM VPN plugin interface
(org.freedesktop.NetworkManager.VPN.Plugin, NM 1.5x):
states 0..6 (UNKNOWN..STOPPED); Failure carries a uint32 code
(0 login-failed, 1 connect-failed, 2 bad-ip-config).

Ownership model: NetworkManager owns routing and DNS.  We never touch
resolv.conf or install routes; NM does that from our Config/Ip4Config
dictionaries.  tun2proxy runs WITHOUT --setup.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Tuple

from . import backend as backend_mod
from .backend import BackendProcess, build_argv, probe_tcp, tun_exists
from .config import ConfigError, VpnConfig

# NMVpnServiceState
STATE_UNKNOWN = 0
STATE_INIT = 1
STATE_SHUTDOWN = 2
STATE_STARTING = 3
STATE_STARTED = 4
STATE_STOPPING = 5
STATE_STOPPED = 6

# NMVpnPluginFailure (D-Bus Failure signal reason codes)
FAIL_LOGIN_FAILED = 0
FAIL_CONNECT_FAILED = 1
FAIL_BAD_IP_CONFIG = 2

Tick = Callable[[], None]


def _ip4_u32(ip: str) -> int:
    """Pack an IPv4 string the way C plugins do: g_variant_new_uint32(s_addr).

    inet_pton leaves the 4 network-order bytes in memory; the C union read on
    a little-endian host therefore equals int.from_bytes(..., 'little').
    """
    import socket
    import sys

    raw = socket.inet_aton(ip)
    return int.from_bytes(raw, "little" if sys.byteorder == "little" else "big")


class BusyConnection(RuntimeError):
    """Connect while a connection attempt/run is already in progress."""


class PluginLogic:
    def __init__(
        self,
        emit: Callable[[str, Dict[str, Tuple[str, object]]], None],
        schedule: Callable[[float, Tick], None],
        spawn_backend: Callable[[list], BackendProcess],
        firewall,
        service_type: str,
        debug: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.emit = emit
        self.schedule = schedule
        self.spawn_backend = spawn_backend
        self.firewall = firewall
        self.service_type = service_type
        self.debug = debug
        self.clock = clock

        self.state = STATE_INIT
        self.cfg: Optional[VpnConfig] = None
        self.backend: Optional[BackendProcess] = None
        self.phys_if = ""
        self._probe_failures = 0
        self._tun_deadline = 0.0
        self._pending: list = []

        self._set_state(STATE_INIT)

    # ------------------------------------------------------------------
    def log(self, msg: str) -> None:
        import sys

        print(f"tetherfuse-plugin: {msg}", file=sys.stderr, flush=True)

    def _set_state(self, state: int) -> None:
        self.state = state
        self.emit("StateChanged", {"state": ("u", state)})

    # ------------------------------------------------------------------
    # D-Bus method implementations
    # ------------------------------------------------------------------
    def need_secrets(self, settings: Dict) -> str:
        # Proxy credentials (if any) are stored in the profile by the user;
        # we never initiate an interactive secrets request.
        return ""

    def connect(self, connection: Dict) -> None:
        if self.state in (STATE_STARTING, STATE_STARTED):
            raise BusyConnection("connection already active")

        cfg = VpnConfig.from_nm_connection(connection, self.service_type)
        self.cfg = cfg
        self._probe_failures = 0
        self._tun_deadline = self.clock() + cfg.probe_attempts * cfg.probe_interval + 5.0
        self.phys_if = _default_route_if()

        self._set_state(STATE_STARTING)
        try:
            self.backend = self.spawn_backend(build_argv(cfg, debug=self.debug))
        except OSError as e:
            self._fail(FAIL_CONNECT_FAILED, f"backend failed to start: {e}")
            return
        pid = getattr(getattr(self.backend, "proc", None), "pid", "?")
        self.log(f"spawned backend pid={pid} "
                 f"tun={cfg.tun_name} proxy={cfg.proxy_host}:{cfg.proxy_port}")
        self._tick()

    def disconnect(self) -> None:
        if self.state in (STATE_STOPPED, STATE_STOPPING):
            return
        self._set_state(STATE_STOPPING)
        self._teardown()
        self._set_state(STATE_STOPPED)

    def new_secrets(self, connection: Dict) -> None:
        # Store updated secrets for the next activation.
        try:
            cfg = VpnConfig.from_nm_connection(connection, self.service_type)
        except ConfigError as e:
            self.log(f"ignoring NewSecrets with bad config: {e}")
            return
        if self.state == STATE_STARTING and self.backend:
            self.log("NewSecrets during activation: restarting backend with new credentials")
            self._teardown()
            self.cfg = cfg
            self.backend = self.spawn_backend(build_argv(cfg, debug=self.debug))
            self._tick()
        else:
            self.cfg = cfg

    def set_failure(self, reason: str) -> None:
        """SetFailure D-Bus method (helper API parity)."""
        code = FAIL_LOGIN_FAILED if ("auth" in reason or "login" in reason) else FAIL_CONNECT_FAILED
        self._fail(code, reason)

    # ------------------------------------------------------------------
    # Internal machinery
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if self.state != STATE_STARTING:
            return
        cfg = self.cfg
        assert cfg is not None

        if self.clock() > self._tun_deadline:
            self._fail(FAIL_CONNECT_FAILED, "timeout waiting for tunnel/proxy readiness")
            return

        if not self.backend.running():
            rc = self.backend.returncode()
            self._fail(FAIL_CONNECT_FAILED, f"backend exited during activation (rc={rc})")
            return

        if not tun_exists(cfg.tun_name):
            self.schedule(0.5, self._tick)
            return

        # TUN is up; verify the proxy actually answers before claiming
        # "connected".  This is the delayed-hotspot-startup retry path.
        if not probe_tcp(cfg.proxy_host, cfg.proxy_port, min(cfg.probe_interval, 3.0)):
            self._probe_failures += 1
            if self._probe_failures >= cfg.probe_attempts:
                self._fail(FAIL_CONNECT_FAILED,
                           f"proxy {cfg.proxy_host}:{cfg.proxy_port} unreachable "
                           f"after {self._probe_failures} attempts")
                return
            self.schedule(cfg.probe_interval, self._tick)
            return

        self._complete()

    def _complete(self) -> None:
        cfg = self.cfg
        assert cfg is not None

        if cfg.fail_closed:
            try:
                self.firewall.apply(cfg, cfg.proxy_host, self.phys_if or "wlan0", cfg.tun_name)
            except Exception as e:  # noqa: BLE001
                self._fail(FAIL_CONNECT_FAILED, f"firewall apply failed: {e}")
                return

        self.emit("Config", {
            "tundev": ("s", cfg.tun_name),
            "gateway": ("u", _ip4_u32(cfg.proxy_host)),  # external gateway = proxy
            "has-ip4": ("b", True),
            "has-ip6": ("b", cfg.ipv6_enabled),
            "can-persist": ("b", False),
            **({"mtu": ("u", cfg.mtu)} if cfg.mtu else {}),
        })

        ip4: Dict[str, Tuple[str, object]] = {
            "address": ("u", _ip4_u32(cfg.tun_address)),
            "ptp": ("u", _ip4_u32(cfg.tun_address)),  # vpnc precedent: PTP == internal address
            "internal-gateway": ("u", _ip4_u32(cfg.tun_address)),
            "prefix": ("u", cfg.tun_prefix),
            "dns": ("au", [_ip4_u32(cfg.virtual_dns_ip)]),
            "never-default": ("b", not cfg.route_all),
        }
        if cfg.extra_routes:
            ip4["routes"] = ("aau", [
                [_ip4_u32(cidr.split("/")[0]), int(cidr.split("/")[1]), 0, 0]
                for cidr in cfg.extra_routes
            ])
        self.emit("Ip4Config", ip4)

        self._set_state(STATE_STARTED)
        self.log(f"STARTED tun={cfg.tun_name} address={cfg.tun_address}/{cfg.tun_prefix} "
                 f"dns={cfg.virtual_dns_ip} fail_closed={cfg.fail_closed}")

    def child_exited(self, returncode: int) -> None:
        """Backend died after we reported STARTED -> report failure honestly."""
        if self.state == STATE_STARTED:
            self.firewall.remove()
            self._set_state(STATE_STOPPING)
            self._teardown(keep_state=True)
            self.emit("Failure", {"reason": ("u", FAIL_CONNECT_FAILED)})
            self._set_state(STATE_STOPPED)
            self.log(f"backend crashed mid-run (rc={returncode}); reported failure to NM")
        elif self.state == STATE_STARTING:
            self._tick()  # normal path inspects liveness itself

    def shutdown(self) -> None:
        """SIGTERM/SIGINT from NM: stop backend, drop rules, exit cleanly."""
        if self.state in (STATE_STARTING, STATE_STARTED):
            self._set_state(STATE_STOPPING)
            self._teardown()
            self._set_state(STATE_STOPPED)

    # ------------------------------------------------------------------
    def _teardown(self, keep_state: bool = False) -> None:
        if self.backend:
            self.backend.stop()
            self.backend = None
        try:
            self.firewall.remove()
        except Exception as e:  # noqa: BLE001
            self.log(f"firewall remove failed: {e}")

    def _fail(self, code: int, detail: str) -> None:
        self.log(f"FAILURE code={code}: {detail}")
        self._teardown()
        self.emit("Failure", {"reason": ("u", code)})
        self._set_state(STATE_STOPPED)


def _default_route_if() -> str:
    """Interface of the current default route (where the proxy is reachable)."""
    try:
        with open("/proc/net/route") as fh:
            fh.readline()
            best = (1 << 32, "")
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                iface, dest, flags, metric = parts[0], parts[1], parts[2], parts[3]
                if dest != "00000000":
                    continue
                m = int(metric, 16)
                if m < best[0]:
                    best = (m, iface)
            return best[1]
    except OSError:
        pass
    return ""
