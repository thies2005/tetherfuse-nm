import os
import socket
import subprocess
import sys
import time

import pytest

import tfvpn.backend as backend_mod
from tfvpn.backend import BackendProcess, build_argv, build_proxy_url, probe_tcp, tun_exists
from tfvpn.config import VpnConfig


def cfg(**over):
    d = {"proxy-host": "192.168.49.1", "proxy-port": "8228"}
    d.update(over)
    return VpnConfig.from_map(d)


def test_proxy_url_plain():
    assert build_proxy_url(cfg()) == "http://192.168.49.1:8228"


def test_proxy_url_auth_percent_encoded():
    c = VpnConfig.from_map(
        {"proxy-host": "p.example", "proxy-port": "8080"},
        secrets={"proxy-username": "john.doe", "proxy-password": "p@ss:w/ord"},
    )
    assert build_proxy_url(c) == "http://john.doe:p%40ss%3Aw%2Ford@p.example:8080"


def test_argv_shape():
    argv = build_argv(cfg())
    assert argv[0] == "/usr/bin/tun2proxy"
    assert ["--tun", "tetherfuse0"] == argv[1:3]
    assert "http://192.168.49.1:8228" in argv
    assert "--dns" in argv and argv[argv.index("--dns") + 1] == "virtual"
    i = argv.index("--virtual-dns-pool")
    assert argv[i + 1] == "198.18.128.0/17"  # allocations must avoid local addrs
    assert "--exit-on-fatal-error" in argv
    assert "--setup" not in argv  # NetworkManager owns routing, never tun2proxy --setup
    assert "-6" not in argv and "--ipv6-enabled" not in argv


def test_argv_options():
    argv = build_argv(cfg(**{
        "dns": "over-tcp", "dns-addr": "1.1.1.1",
        "bypass-cidrs": "192.168.0.0/16,10.0.0.0/8",
        "ipv6-enabled": "true",
    }), debug=True)
    i = argv.index("--dns-addr")
    assert argv[i + 1] == "1.1.1.1"
    i = argv.index("--mtu")
    assert argv[i + 1] == "1400"
    i = argv.index("--max-sessions")
    assert argv[i + 1] == "4096"  # upstream default 200 is too small system-wide
    assert "--virtual-dns-pool" not in argv  # only meaningful in virtual mode
    bypasses = [argv[i + 1] for i, a in enumerate(argv) if a == "--bypass"]
    assert bypasses == ["192.168.0.0/16", "10.0.0.0/8"]
    assert "--ipv6-enabled" in argv
    i = argv.index("--verbosity")
    assert argv[i + 1] == "4"  # bare -v requires a level in v0.8.4


def test_tun_exists_via_env(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_mod, "PROC_NET", str(tmp_path))
    assert not tun_exists("tetherfuse0")
    (tmp_path / "tetherfuse0").touch()
    assert tun_exists("tetherfuse0")


def test_probe_tcp(tmp_port_factory):
    port = tmp_port_factory()
    assert probe_tcp("127.0.0.1", port, 0.5) is True
    # closed port: kernel refuses immediately on loopback
    closed = port + 1
    assert probe_tcp("127.0.0.1", closed, 0.5) is False


@pytest.fixture
def tmp_port_factory():
    holders = []

    def make():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        holders.append(s)
        return s.getsockname()[1]

    yield make


def test_process_lifecycle(tmp_path):
    stub = os.path.join(os.path.dirname(__file__), "stub_backend.py")
    argv = [sys.executable, stub]
    env = dict(os.environ, TFVPN_SYSFS_NET=str(tmp_path))
    proc = BackendProcess(argv, env=env)
    proc.spawn()
    assert proc.running() is True
    marker = tmp_path / "tetherfuse0"
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    proc.stop(timeout=5)
    assert proc.running() is False
