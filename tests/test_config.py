import pytest

from tfvpn.config import ConfigError, VpnConfig


def base(over=None):
    d = {"proxy-host": "192.168.49.1", "proxy-port": "8228"}
    d.update(over or {})
    return d


def test_defaults():
    cfg = VpnConfig.from_map(base())
    assert cfg.proxy_host == "192.168.49.1"
    assert cfg.proxy_port == 8228
    assert cfg.tun_name == "tetherfuse0"
    assert cfg.tun_address == "198.18.0.1"
    assert cfg.tun_prefix == 15
    assert cfg.dns == "virtual"
    assert cfg.route_all is True
    assert cfg.fail_closed is False
    assert cfg.ipv6_enabled is False
    assert cfg.backend_bin == "/usr/bin/tun2proxy"


def test_full_parse():
    cfg = VpnConfig.from_map(base({
        "tun-name": "tun9", "tun-address": "10.5.0.1", "tun-prefix": "24",
        "virtual-dns-ip": "10.5.0.2", "virtual-dns-pool": "10.5.0.128/25",
        "mtu": "1300", "dns": "over-tcp", "dns-addr": "1.1.1.1",
        "route-all": "false", "extra-routes": "10.0.0.0/8, 172.16.0.0/12",
        "bypass-cidrs": "192.168.0.0/16", "fail-closed": "true",
        "probe-attempts": "3", "probe-interval": "1.5",
        "backend-bin": "/usr/local/bin/tun2proxy", "ipv6-enabled": "true",
    }))
    assert cfg.tun_name == "tun9"
    assert cfg.tun_prefix == 24
    assert cfg.virtual_dns_ip == "10.5.0.2"
    assert cfg.virtual_dns_pool == "10.5.0.128/25"
    assert cfg.route_all is False
    assert cfg.extra_routes == ["10.0.0.0/8", "172.16.0.0/12"]
    assert cfg.fail_closed is True
    assert cfg.ipv6_enabled is True
    assert cfg.dns_addr == "1.1.1.1"


def test_injection_rejected():
    for bad in ["1.2.3.4; rm -rf /", "host\nDROP", "$(reboot)", "a b", ""]:
        with pytest.raises(ConfigError):
            VpnConfig.from_map(base({"proxy-host": bad}))


def test_port_bounds():
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"proxy-port": "0"}))
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"proxy-port": "70000"}))
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"proxy-port": "8228x"}))


def test_tun_name_rules():
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"tun-name": "way-too-long-interface-name"}))
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"tun-name": "1begins-with-digit"}))
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"tun-name": "bad;chars"}))


def test_bad_cidrs():
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"extra-routes": "300.1.1.0/8"}))
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"bypass-cidrs": "not-a-cidr"}))


def test_dns_mode():
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base({"dns": "magic"}))


def test_secrets_and_control_chars():
    cfg = VpnConfig.from_map(base(), secrets={"proxy-password": "s3cret"})
    assert cfg.proxy_password == "s3cret"
    with pytest.raises(ConfigError):
        VpnConfig.from_map(base(), secrets={"proxy-password": "bad\x01pass"})


def test_dns_server_must_differ_from_tun_address():
    with pytest.raises(ConfigError, match="virtual-dns-ip"):
        VpnConfig.from_map(base({"virtual-dns-ip": "198.18.0.1"}))
    cfg = VpnConfig.from_map(base())
    assert cfg.virtual_dns_ip == "198.18.0.2"


def test_virtual_dns_pool_must_avoid_local_addresses():
    # upstream default pool would allocate 198.18.0.1 (the interface) to a domain
    with pytest.raises(ConfigError, match="must not lie inside"):
        VpnConfig.from_map(base({"virtual-dns-pool": "198.18.0.0/15"}))
    # DNS server inside the pool is equally fatal
    with pytest.raises(ConfigError, match="must not lie inside"):
        VpnConfig.from_map(base({"virtual-dns-pool": "198.18.0.0/24"}))
    # pool must be on-link via the tun prefix (198.20.x is outside 198.18.0.0/15)
    with pytest.raises(ConfigError, match="subnet"):
        VpnConfig.from_map(base({"virtual-dns-pool": "198.20.0.0/16"}))
    cfg = VpnConfig.from_map(base())
    assert cfg.virtual_dns_pool == "198.18.128.0/17"


def test_from_nm_connection():
    conn = {
        "connection": {"id": "TetherFuse"},
        "vpn": {
            "service-type": "org.freedesktop.NetworkManager.tetherfuse",
            "data": base(),
            "secrets": {"proxy-password": "pw"},
        },
    }
    cfg = VpnConfig.from_nm_connection(conn, "org.freedesktop.NetworkManager.tetherfuse")
    assert cfg.proxy_port == 8228
    assert cfg.proxy_password == "pw"
    with pytest.raises(ConfigError):
        VpnConfig.from_nm_connection(conn, "org.example.other")
