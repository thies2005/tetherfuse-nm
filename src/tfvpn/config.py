"""Configuration parsing and validation for the TetherFuse NM VPN plugin.

Values live in the NetworkManager connection profile: the ``vpn`` setting's
``data`` dict (per-connection, non-secret) and ``secrets`` dict (proxy
password).  Validation is deliberately strict: every value must match a
whitelist pattern before it reaches a subprocess argument list.  We never
build shell command lines from these values.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class ConfigError(ValueError):
    """Raised when the connection profile contains unusable values."""


_RE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
_RE_TUN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,14}$")
_RE_ABSPATH = re.compile(r"^/[A-Za-z0-9._/+~-]+$")
_RE_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")
_RE_CTRL = re.compile(r"[\x00-\x1f\x7f]")

DNS_MODES = ("virtual", "over-tcp", "direct")

DEFAULTS: Dict[str, str] = {
    "proxy-username": "",
    "proxy-password": "",
    "dns-addr": "",
    "tun-name": "tetherfuse0",
    "tun-address": "198.18.0.1",
    "tun-prefix": "15",
    # Must differ from tun-address: a DNS server equal to the interface's
    # own address gets packets delivered locally (nothing listens -> refused)
    # instead of routed into the TUN where tun2proxy's virtual DNS answers.
    "virtual-dns-ip": "198.18.0.2",
    # tun2proxy allocates fake IPs for resolved names from this pool. The
    # default (198.18.0.0/15) would hand out 198.18.0.1 (our interface
    # address) to the first resolved domain -> connections to it are
    # delivered locally. Keep the interface/DNS addresses below the pool.
    "virtual-dns-pool": "198.18.128.0/17",
    "mtu": "1400",
    "dns": "virtual",
    "route-all": "true",
    "extra-routes": "",
    "bypass-cidrs": "",
    "fail-closed": "false",
    "fail-closed-allow-mdns": "false",
    "probe-attempts": "8",
    "probe-interval": "2",
    "max-sessions": "4096",
    "backend-bin": "/usr/bin/tun2proxy",
    "ipv6-enabled": "false",
}

_BOOL_KEYS = {"route-all", "fail-closed", "fail-closed-allow-mdns", "ipv6-enabled"}
_LIST_KEYS = {"extra-routes", "bypass-cidrs"}
_INT_KEYS = {"proxy-port", "tun-prefix", "mtu", "probe-attempts"}
_FLOAT_KEYS = {"probe-interval"}


@dataclass
class VpnConfig:
    proxy_host: str
    proxy_port: int
    proxy_username: str = ""
    proxy_password: str = ""
    tun_name: str = "tetherfuse0"
    tun_address: str = "198.18.0.1"
    tun_prefix: int = 15
    virtual_dns_ip: str = "198.18.0.1"
    mtu: int = 1400
    dns: str = "virtual"
    dns_addr: str = ""
    route_all: bool = True
    extra_routes: List[str] = field(default_factory=list)
    bypass_cidrs: List[str] = field(default_factory=list)
    fail_closed: bool = False
    fail_closed_allow_mdns: bool = False
    probe_attempts: int = 8
    probe_interval: float = 2.0
    max_sessions: int = 4096
    virtual_dns_pool: str = "198.18.128.0/17"
    backend_bin: str = "/usr/bin/tun2proxy"
    ipv6_enabled: bool = False

    # ------------------------------------------------------------------
    @classmethod
    def from_map(cls, data: Dict[str, str], secrets: Optional[Dict[str, str]] = None) -> "VpnConfig":
        secrets = secrets or {}
        merged: Dict[str, str] = dict(DEFAULTS)
        for key, value in data.items():
            if not isinstance(value, str):
                raise ConfigError(f"vpn.data key {key!r}: value must be a string")
            merged[key] = value

        def raw(key: str) -> str:
            if key not in merged and key not in DEFAULTS:
                raise ConfigError(f"vpn.data: missing required key {key!r}")
            return merged[key] if key in merged else DEFAULTS[key]

        def s(key: str) -> str:
            v = raw(key)
            # List-valued keys are comma-separated; spaces around commas are
            # fine, each element is validated strictly by cidrs().
            pattern = _RE_CTRL if key in _LIST_KEYS else _RE_UNSAFE
            if pattern.search(v):
                raise ConfigError(f"vpn.data key {key!r}: control/whitespace characters not allowed")
            return v

        def b(key: str) -> bool:
            v = raw(key).strip().lower()
            if v not in ("true", "false"):
                raise ConfigError(f"vpn.data key {key!r}: expected true/false, got {v!r}")
            return v == "true"

        def i(key: str, lo: int, hi: int) -> int:
            r = raw(key).strip()
            if not r.isdigit():
                raise ConfigError(f"vpn.data key {key!r}: expected integer, got {r!r}")
            n = int(r)
            if not (lo <= n <= hi):
                raise ConfigError(f"vpn.data key {key!r}: {n} out of range [{lo}, {hi}]")
            return n

        def f(key: str, lo: float, hi: float) -> float:
            r = raw(key).strip()
            try:
                n = float(r)
            except ValueError:
                raise ConfigError(f"vpn.data key {key!r}: expected number, got {r!r}")
            if not (lo <= n <= hi):
                raise ConfigError(f"vpn.data key {key!r}: {n} out of range [{lo}, {hi}]")
            return n

        def cidrs(key: str) -> List[str]:
            raw = s(key).strip()
            if not raw:
                return []
            out = []
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    net = ipaddress.ip_network(part, strict=False)
                except ValueError as e:
                    raise ConfigError(f"vpn.data key {key!r}: bad CIDR {part!r}: {e}")
                if net.version != 4:
                    raise ConfigError(f"vpn.data key {key!r}: only IPv4 CIDRs supported: {part!r}")
                out.append(str(net))
            return out

        host = s("proxy-host")
        if not host or not _RE_HOST.match(host):
            raise ConfigError(f"vpn.data 'proxy-host': invalid host {host!r}")
        port = i("proxy-port", 1, 65535)

        username = secrets.get("proxy-username", s("proxy-username"))
        password = secrets.get("proxy-password", s("proxy-password"))
        for name, val in (("proxy-username", username), ("proxy-password", password)):
            if _RE_UNSAFE.search(val):
                raise ConfigError(f"{name}: control characters not allowed")

        tun_name = s("tun-name")
        if not _RE_TUN.match(tun_name):
            raise ConfigError(f"vpn.data 'tun-name': invalid interface name {tun_name!r}")

        def ipv4(key: str, default: str) -> str:
            raw = s(key) or default
            try:
                addr = ipaddress.ip_address(raw)
            except ValueError:
                raise ConfigError(f"vpn.data {key!r}: invalid IPv4 address {raw!r}")
            if addr.version != 4:
                raise ConfigError(f"vpn.data {key!r}: IPv4 required")
            return str(addr)

        tun_address = ipv4("tun-address", DEFAULTS["tun-address"])
        virtual_dns_ip = ipv4("virtual-dns-ip", DEFAULTS["virtual-dns-ip"])
        if virtual_dns_ip == tun_address:
            raise ConfigError(
                "vpn.data 'virtual-dns-ip' must differ from 'tun-address': "
                "queries to the interface's own address never enter the TUN device, "
                "so tun2proxy's virtual DNS would never see them")

        pool_raw = (merged.get("virtual-dns-pool") or DEFAULTS["virtual-dns-pool"]).strip()
        try:
            pool = ipaddress.ip_network(pool_raw, strict=False)
        except ValueError as e:
            raise ConfigError(f"vpn.data 'virtual-dns-pool': bad CIDR {pool_raw!r}: {e}")
        if pool.version != 4:
            raise ConfigError("vpn.data 'virtual-dns-pool': IPv4 CIDR required")
        on_link = ipaddress.ip_network(f"{tun_address}/{i('tun-prefix', 1, 32)}", strict=False)
        if ipaddress.ip_address(virtual_dns_ip) not in on_link:
            raise ConfigError(
                f"vpn.data 'virtual-dns-ip' ({virtual_dns_ip}) must be inside the "
                f"on-link prefix {on_link} so queries route into the tunnel")
        if not pool.subnet_of(on_link):
            raise ConfigError(
                f"vpn.data 'virtual-dns-pool' {pool} must be a subnet of the on-link "
                f"prefix {on_link} so fake IPs route into the tunnel")
        for label, ip in (("tun-address", tun_address), ("virtual-dns-ip", virtual_dns_ip)):
            if ipaddress.ip_address(ip) in pool:
                raise ConfigError(
                    f"vpn.data {label!r} ({ip}) must not lie inside 'virtual-dns-pool' "
                    f"{pool}: tun2proxy would allocate it to a resolved domain")

        dns_mode = s("dns") or "virtual"
        if dns_mode not in DNS_MODES:
            raise ConfigError(f"vpn.data 'dns': must be one of {DNS_MODES}")
        dns_addr = ipv4("dns-addr", "") if s("dns-addr") else ""

        backend = s("backend-bin") or DEFAULTS["backend-bin"]
        if not _RE_ABSPATH.match(backend):
            raise ConfigError(f"vpn.data 'backend-bin': absolute path required, got {backend!r}")

        return cls(
            proxy_host=host,
            proxy_port=port,
            proxy_username=username,
            proxy_password=password,
            tun_name=tun_name,
            tun_address=tun_address,
            tun_prefix=i("tun-prefix", 1, 32),
            virtual_dns_ip=virtual_dns_ip,
            mtu=i("mtu", 0, 65535),
            dns=dns_mode,
            dns_addr=dns_addr,
            route_all=b("route-all"),
            extra_routes=cidrs("extra-routes"),
            bypass_cidrs=cidrs("bypass-cidrs"),
            fail_closed=b("fail-closed"),
            fail_closed_allow_mdns=b("fail-closed-allow-mdns"),
            probe_attempts=i("probe-attempts", 1, 30),
            probe_interval=f("probe-interval", 0.5, 10.0),
            max_sessions=i("max-sessions", 16, 1_000_000),
            virtual_dns_pool=str(pool),
            backend_bin=backend,
            ipv6_enabled=b("ipv6-enabled"),
        )

    # ------------------------------------------------------------------
    @classmethod
    def from_nm_connection(cls, connection: Dict, service_type: str) -> "VpnConfig":
        """Build a config from an unpacked NM connection dict (a{sa{sv}} -> py)."""
        conn = connection.get("connection", {})
        vpn = connection.get("vpn", {})
        st = vpn.get("service-type", "")
        if st != service_type:
            raise ConfigError(f"connection service-type {st!r} != {service_type!r}")
        data = vpn.get("data", {})
        secrets = vpn.get("secrets", {})
        if not isinstance(data, dict):
            raise ConfigError("vpn.data missing or malformed")
        return cls.from_map(data, secrets if isinstance(secrets, dict) else {})
