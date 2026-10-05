"""tun2proxy backend process management.

The backend creates and owns the TUN device; NetworkManager (not tun2proxy's
``--setup`` mode) installs addresses and routes based on the Config/Ip4Config
dictionaries we report.  We deliberately pass ``--exit-on-fatal-error`` so a
dead proxy path terminates the backend and lets us report failure to NM
instead of pretending the tunnel is healthy.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
from typing import List, Optional
from urllib.parse import quote

from .config import VpnConfig

# Overridable for tests so we never touch the real /sys.
PROC_NET = os.environ.get("TFVPN_SYSFS_NET", "/sys/class/net")


def build_proxy_url(cfg: VpnConfig) -> str:
    auth = ""
    if cfg.proxy_username:
        user = quote(cfg.proxy_username, safe="")
        if cfg.proxy_password:
            user += ":" + quote(cfg.proxy_password, safe="")
        auth = user + "@"
    return f"http://{auth}{cfg.proxy_host}:{cfg.proxy_port}"


def build_argv(cfg: VpnConfig, debug: bool = False) -> List[str]:
    # Flags verified against the real tun2proxy v0.8.4 x86_64 release binary.
    argv = [
        cfg.backend_bin,
        "--tun", cfg.tun_name,
        "--proxy", build_proxy_url(cfg),
        "--dns", cfg.dns,
    ]
    if cfg.dns_addr:
        argv += ["--dns-addr", cfg.dns_addr]
    if cfg.dns == "virtual":
        # Keep allocations away from the interface/DNS addresses (which sit
        # below 198.18.128.0 by default and are validated in config.py).
        argv += ["--virtual-dns-pool", cfg.virtual_dns_pool]
    if cfg.mtu:
        argv += ["--mtu", str(cfg.mtu)]
    # Upstream default is 200 — sized for one app, not a system-wide default
    # route. Verified live: a desktop behind the tunnel hits 200 in ~1 min
    # and tun2proxy force-exits, which NM reports as VPN failure.
    argv += ["--max-sessions", str(cfg.max_sessions)]
    for cidr in cfg.bypass_cidrs:
        argv += ["--bypass", cidr]
    argv.append("--exit-on-fatal-error")
    if cfg.ipv6_enabled:
        argv.append("--ipv6-enabled")
    if debug:
        argv += ["--verbosity", "4"]
    return argv


def tun_exists(name: str) -> bool:
    return os.path.exists(os.path.join(PROC_NET, name))


def probe_tcp(host: str, port: int, timeout: float) -> bool:
    """True if a TCP connection to the proxy can be established right now."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class BackendProcess:
    """Supervises one tun2proxy run.  One at a time; never concurrent."""

    def __init__(self, argv: List[str], env: Optional[dict] = None):
        self.argv = argv
        self.env = env
        self.proc: Optional[subprocess.Popen] = None

    def spawn(self) -> int:
        self.proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,  # inherit: tun2proxy's stderr lands in the journal via NM
            start_new_session=True,
            close_fds=True,
            env=self.env,
        )
        return self.proc.pid

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self.proc.poll() if self.proc else None

    def stop(self, timeout: float = 5.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        # The TUN device disappears when the process holding it exits.
