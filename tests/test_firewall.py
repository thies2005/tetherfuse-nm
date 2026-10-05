import pytest

from tfvpn.config import VpnConfig
from tfvpn.firewall import Firewall, ruleset


def cfg(over=None):
    d = {"proxy-host": "192.168.49.1", "proxy-port": "8228"}
    d.update(over or {})
    return VpnConfig.from_map(d)


def test_ruleset_scoping():
    text = ruleset(cfg(), "192.168.49.1", "wlp2s0", "tetherfuse0")
    assert text.startswith("table inet tetherfuse_vpn {")
    # first functional rule must exempt every other interface (docker/tailscale/lo safe)
    first_rule = [l for l in text.splitlines() if l.strip().startswith("oifname")][0]
    assert first_rule.strip().startswith('oifname != "wlp2s0" counter accept')
    # proxy TCP allow, DHCP renewal, NDP
    assert "ip daddr 192.168.49.1 tcp dport 8228" in text
    assert "udp dport 67" in text
    assert "ipv6-icmp" in text
    # unsupported UDP must fail fast so QUIC falls back to TCP
    assert "reject with icmpx port-unreachable" in text
    # final fail-closed drop scoped to the hotspot interface
    assert 'counter drop comment "tetherfuse: fail-closed on wlp2s0"' in text
    # we never flush or touch foreign tables
    assert "flush" not in text
    assert text.count("table inet tetherfuse_vpn {") == 1


def test_ruleset_options():
    text = ruleset(cfg({"bypass-cidrs": "192.168.0.0/16",
                        "fail-closed-allow-mdns": "true"}),
                   "192.168.49.1", "wlp2s0", "tetherfuse0")
    assert "ip daddr 192.168.0.0/16" in text
    assert "224.0.0.251" in text  # mDNS allowed
    plain = ruleset(cfg(), "192.168.49.1", "wlp2s0", "tetherfuse0")
    assert "224.0.0.251" not in plain


def test_unsafe_iface_rejected():
    with pytest.raises(ValueError):
        ruleset(cfg(), "192.168.49.1", 'bad"; flush ruleset; #', "tetherfuse0")
    with pytest.raises(ValueError):
        ruleset(cfg(), "192.168.49.1", "wlp2s0", 'x; drop')


def test_firewall_dry_run():
    fw = Firewall(dry_run=True)
    c = cfg({"fail-closed": "true"})
    fw.apply(c, "192.168.49.1", "wlp2s0", "tetherfuse0")
    fw.remove()
    joined = "\n".join(fw.commands)
    assert "delete table inet tetherfuse_vpn" in joined
    assert "nft -f" in joined


def test_firewall_disabled_by_default():
    fw = Firewall(dry_run=True)
    fw.apply(cfg(), "192.168.49.1", "wlp2s0", "tetherfuse0")
    assert fw.commands == []  # fail_closed=false -> no rules at all


def test_firewall_real_apply(monkeypatch):
    """Apply/remove against the live nft binary if CAP_NET_ADMIN, else skip."""
    import shutil
    import subprocess

    if not shutil.which("nft"):
        pytest.skip("nft not available")
    probe = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True)
    if probe.returncode != 0 or "Operation not permitted" in probe.stderr:
        pytest.skip("no CAP_NET_ADMIN for live nft test")

    fw = Firewall()
    before = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True)
    fw.apply(cfg({"fail-closed": "true"}), "192.168.49.1", "wlp2s0", "tetherfuse0")
    during = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True)
    fw.remove()
    after = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True)
    assert "tetherfuse_vpn" in during.stdout
    assert "tetherfuse_vpn" not in after.stdout
    # no foreign table appeared or disappeared
    def tables(out):
        return set(l.strip() for l in out.stdout.splitlines() if l.strip())
    assert tables(before) == tables(after)
