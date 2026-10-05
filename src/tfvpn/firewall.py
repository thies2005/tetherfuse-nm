"""Scoped nftables fail-closed rules for the TetherFuse integration.

Design constraints (from the integration spec):

* One private table ``inet tetherfuse_vpn`` — nothing else is ever flushed,
  deleted or modified; Docker/UFW/tailscale/other-VPN rules are untouched.
* All rules live in a single ``output`` hook chain and match ONLY traffic
  leaving the hotspot's physical interface, so every other interface
  (loopback, docker0, tailscale0, ethernet, ...) is accepted by the first
  rule and completely unaffected.
* Unsupported UDP (QUIC/WebRTC/games) is REJECTED, not silently dropped:
  applications get an ICMP port-unreachable immediately and fall back to
  TCP.  Direct UDP to the internet never leaks around the tunnel.
* The proxy TCP connection, DHCP renewal, NDP/ICMPv6 and (optionally) mDNS
  stay allowed.  Everything else on the hotspot interface is dropped.
* Idempotent: apply destroys only our own table (if present) and recreates
  it; remove deletes only our own table.
"""
from __future__ import annotations

import subprocess
from typing import List, Optional

from .config import VpnConfig

TABLE = "tetherfuse_vpn"

QUOTED_SAFE_IFACE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def _quote_iface(name: str) -> str:
    if not name or any(c not in QUOTED_SAFE_IFACE_CHARS for c in name):
        raise ValueError(f"unsafe interface name {name!r}")
    return name


def ruleset(cfg: VpnConfig, proxy_ip: str, phys_if: str, tun: str) -> str:
    """Render the nftables ruleset as text.  Fails closed on ``phys_if`` only."""
    phys = _quote_iface(phys_if)
    tun_q = _quote_iface(tun)
    lines = [
        f"table inet {TABLE} {{",
        "  chain output {",
        "    type filter hook output priority filter; policy accept;",
        f'    oifname != "{phys}" counter accept comment "tetherfuse: other interfaces untouched"',
        f'    oifname "{tun_q}" counter accept comment "tetherfuse: tunnel traffic"',
        f"    ip daddr {proxy_ip} tcp dport {cfg.proxy_port} counter accept comment \"tetherfuse: proxy\"",
        "    udp dport 67 counter accept comment \"tetherfuse: DHCP renewal\"",
        "    meta nfproto ipv6 meta l4proto ipv6-icmp counter accept comment \"tetherfuse: NDP/ICMPv6\"",
        "    ip6 daddr fe80::/10 counter accept comment \"tetherfuse: IPv6 link-local\"",
        "    udp dport 546 counter accept comment \"tetherfuse: DHCPv6 client\"",
    ]
    if cfg.fail_closed_allow_mdns:
        lines.append(
            '    ip daddr 224.0.0.251 udp dport 5353 counter accept comment "tetherfuse: mDNS"'
        )
    for cidr in cfg.bypass_cidrs:
        lines.append(f"    ip daddr {cidr} counter accept comment \"tetherfuse: local bypass\"")
    lines += [
        "    meta l4proto udp counter reject with icmpx port-unreachable"
        ' comment "tetherfuse: unsupported UDP fails fast (QUIC->TCP fallback)"',
        f'    counter drop comment "tetherfuse: fail-closed on {phys}"',
        "  }",
        "}",
        "",
    ]
    return "\n".join(lines)


class Firewall:
    """Apply/remove the scoped table.  ``dry_run=True`` records commands only."""

    def __init__(self, dry_run: bool = False, runner=None):
        self.dry_run = dry_run
        self.commands: List[str] = []
        self._run = runner or self._nft

    @staticmethod
    def _nft(args: List[str], input_text: Optional[str] = None):
        return subprocess.run(
            ["nft", *args], input=input_text, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    # ------------------------------------------------------------------
    def apply(self, cfg: VpnConfig, proxy_ip: str, phys_if: str, tun: str) -> None:
        if not cfg.fail_closed:
            return
        text = ruleset(cfg, proxy_ip, phys_if, tun)
        if self.dry_run:
            self.commands.append(f"nft delete table inet {TABLE} (ignore absent)")
            self.commands.append("nft -f - <<<'{}'".format(text))
            return
        # Only ever touches our own table.
        self._run(["delete", "table", "inet", TABLE])
        res = self._run(["-f", "-"], input_text=text)
        if res.returncode != 0:
            raise RuntimeError(f"nft load failed: {res.stderr.strip()}")

    def remove(self) -> None:
        if self.dry_run:
            self.commands.append(f"nft delete table inet {TABLE} (ignore absent)")
            return
        self._run(["delete", "table", "inet", TABLE])
