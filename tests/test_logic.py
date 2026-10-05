"""Unit tests for the pure connection state machine (no GLib/D-Bus)."""
import os
import socket

import pytest

import tfvpn.backend as backend_mod
from tfvpn.config import ConfigError
from tfvpn.logic import (
    FAIL_CONNECT_FAILED,
    STATE_STARTED,
    STATE_STARTING,
    STATE_STOPPED,
    BusyConnection,
    PluginLogic,
)

SVC = "org.test.vpn"


def _u32(ip):
    return int.from_bytes(socket.inet_aton(ip), "little")


class ManualScheduler:
    def __init__(self):
        self.queue = []

    def __call__(self, delay, fn):
        self.queue.append(fn)

    def step(self):
        if not self.queue:
            return False
        self.queue.pop(0)()
        return True

    def run(self, max_steps=200):
        n = 0
        while self.step() and n < max_steps:
            n += 1
        return n


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


class FakeBackend:
    """Behaves like BackendProcess; creates the tun marker at spawn()."""

    def __init__(self, netdir, tun, dead=False, no_tun=False):
        self.netdir, self.tun = netdir, tun
        self.dead = dead
        self.no_tun = no_tun
        self.stopped = False
        self.argv = None
        self._rc = 0

    def spawn(self):
        self.argv = ["fake"]
        if not self.dead and not self.no_tun:
            open(os.path.join(self.netdir, self.tun), "w").close()
        return 4242

    def running(self):
        return not self.dead and not self.stopped

    def returncode(self):
        return None if self.running() else (self._rc or 9)

    def kill(self):
        self.dead = True
        self._rc = 1

    def stop(self, timeout=5.0):
        self.stopped = True


class FakeFirewall:
    def __init__(self):
        self.applied = []
        self.removed = 0

    def apply(self, cfg, proxy_ip, phys_if, tun):
        self.applied.append((proxy_ip, phys_if, tun))

    def remove(self):
        self.removed += 1


@pytest.fixture
def netdir(tmp_path, monkeypatch):
    d = tmp_path / "net"
    d.mkdir()
    monkeypatch.setattr(backend_mod, "PROC_NET", str(d))
    return d


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    yield s.getsockname()[1]
    s.close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_logic(netdir, listener_port, backend=None, **cfg_over):
    emits = []
    sched = ManualScheduler()
    clock = FakeClock()
    fw = FakeFirewall()
    backend = backend or FakeBackend(netdir, "tetherfuse0")

    def spawn(argv):
        backend.spawn()
        backend.argv = argv
        return backend

    logic = PluginLogic(
        emit=lambda name, body: emits.append((name, body)),
        schedule=sched,
        spawn_backend=spawn,
        firewall=fw,
        service_type=SVC,
        clock=clock,
    )
    return logic, emits, sched, fw, backend, clock


def conn_dict(port, over=None):
    data = {
        "proxy-host": "127.0.0.1",
        "proxy-port": str(port),
        "backend-bin": "/bin/true",
        "probe-attempts": "8",
        "probe-interval": "0.5",
    }
    data.update(over or {})
    return {
        "connection": {"id": "TetherFuse"},
        "vpn": {"service-type": SVC, "data": data, "secrets": {}},
    }


def signals(emits, name):
    return [b for n, b in emits if n == name]


def states(emits):
    return [b["state"][1] for n, b in emits if n == "StateChanged"]


# ---------------------------------------------------------------- happy path
def test_happy_path(netdir, listener):
    logic, emits, sched, fw, backend, _ = make_logic(netdir, listener)
    logic.connect(conn_dict(listener))
    assert states(emits)[-1] == STATE_STARTED
    assert backend.stopped is False
    # Config dictionary
    cfg_sig = signals(emits, "Config")
    assert cfg_sig, "Config signal missing"
    body = cfg_sig[-1]
    assert body["tundev"] == ("s", "tetherfuse0")
    assert body["gateway"] == ("u", _u32("127.0.0.1"))
    assert body["has-ip4"] == ("b", True)
    assert body["has-ip6"] == ("b", False)
    # Ip4Config dictionary
    ip4 = signals(emits, "Ip4Config")[-1]
    assert ip4["address"] == ("u", _u32("198.18.0.1"))
    assert ip4["ptp"] == ("u", _u32("198.18.0.1"))
    assert ip4["internal-gateway"] == ("u", _u32("198.18.0.1"))
    assert ip4["prefix"] == ("u", 15)
    assert ip4["dns"] == ("au", [_u32("198.18.0.2")])  # distinct from tun address
    assert ip4["never-default"] == ("b", False)
    # fail_closed off by default: no firewall touched
    assert fw.applied == []


def test_split_tunnel(netdir, listener):
    logic, emits, *_ = make_logic(netdir, listener)
    logic.connect(conn_dict(listener, {"route-all": "false",
                                       "extra-routes": "10.0.0.0/8"}))
    ip4 = signals(emits, "Ip4Config")[-1]
    assert ip4["never-default"] == ("b", True)
    assert ip4["routes"] == ("aau", [[_u32("10.0.0.0"), 8, 0, 0]])


def test_fail_closed_applies_firewall(netdir, listener):
    logic, emits, sched, fw, *_ = make_logic(netdir, listener)
    logic.connect(conn_dict(listener, {"fail-closed": "true"}))
    assert len(fw.applied) == 1
    proxy_ip, phys_if, tun = fw.applied[0]
    assert proxy_ip == "127.0.0.1"
    assert tun == "tetherfuse0"


def test_busy_rejected(netdir, listener):
    logic, *_ = make_logic(netdir, listener)
    logic.connect(conn_dict(listener))
    with pytest.raises(BusyConnection):
        logic.connect(conn_dict(listener))


def test_disconnect(netdir, listener):
    logic, emits, sched, fw, backend, _ = make_logic(netdir, listener)
    logic.connect(conn_dict(listener))
    logic.disconnect()
    assert states(emits)[-1] == STATE_STOPPED
    assert backend.stopped is True
    assert fw.removed >= 1


# ---------------------------------------------------------------- failures
def test_proxy_unreachable_bounded(netdir):
    port = closed_port()
    logic, emits, sched, *_ = make_logic(netdir, port)
    logic.connect(conn_dict(port, {"probe-attempts": "2", "probe-interval": "0.5"}))
    sched.run()
    failures = signals(emits, "Failure")
    assert failures == [{"reason": ("u", FAIL_CONNECT_FAILED)}]
    assert states(emits)[-1] == STATE_STOPPED


def test_backend_dies_during_activation(netdir, listener):
    dead = FakeBackend(netdir, "tetherfuse0", dead=True)
    logic, emits, *_ = make_logic(netdir, listener, backend=dead)
    logic.connect(conn_dict(listener))
    failures = signals(emits, "Failure")
    assert failures and failures[0]["reason"] == ("u", FAIL_CONNECT_FAILED)
    assert states(emits)[-1] == STATE_STOPPED


def test_backend_crash_after_started(netdir, listener):
    logic, emits, sched, fw, backend, _ = make_logic(netdir, listener)
    logic.connect(conn_dict(listener))
    assert logic.state == STATE_STARTED
    backend.kill()
    logic.child_exited(1)
    assert signals(emits, "Failure")[-1]["reason"] == ("u", FAIL_CONNECT_FAILED)
    assert states(emits)[-1] == STATE_STOPPED
    assert fw.removed >= 1


def test_readiness_timeout(netdir, listener):
    no_tun = FakeBackend(netdir, "tetherfuse0", no_tun=True)
    logic, emits, sched, _, _, clock = make_logic(netdir, listener, backend=no_tun)
    logic.connect(conn_dict(listener, {"probe-attempts": "1", "probe-interval": "0.5"}))
    clock.advance(30)  # blow past the deadline
    sched.run()
    assert signals(emits, "Failure")[-1]["reason"] == ("u", FAIL_CONNECT_FAILED)
    assert states(emits)[-1] == STATE_STOPPED


def test_bad_config_raises(netdir, listener):
    logic, *_ = make_logic(netdir, listener)
    bad = conn_dict(listener)
    bad["vpn"]["service-type"] = "org.other"
    with pytest.raises(ConfigError):
        logic.connect(bad)
    # the failed attempt must not have started anything
    assert logic.state == 1  # STATE_INIT


def test_need_secrets_empty(netdir, listener):
    logic, *_ = make_logic(netdir, listener)
    assert logic.need_secrets({}) == ""
