"""Live D-Bus test: runs the real plugin on the SESSION bus with a stub
backend and drives it exactly the way NetworkManager does — Connect() with an
a{sa{sv}} connection, then watches for Config/Ip4Config/StateChanged signals,
then Disconnect().

This verifies the entire D-Bus layer (introspection XML, method dispatch,
variant packing, signal emission) without needing root or a TUN device:
TFVPN_SYSFS_NET redirects the tun liveness check to a temp directory and the
stub backend "creates" the device there.
"""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

gi = pytest.importorskip("gi")
from gi.repository import GLib, Gio  # noqa: E402

HERE = os.path.dirname(__file__)
SRC = os.path.join(HERE, "..", "src")
OBJECT_PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
INTERFACE = "org.freedesktop.NetworkManager.VPN.Plugin"

_NAME_SEQ = [0]


@pytest.fixture()
def bus_name():
    # Unique per test: avoids races where the previous plugin process still
    # owns the name while the next one starts (async release after SIGTERM).
    _NAME_SEQ[0] += 1
    return f"org.freedesktop.NetworkManager.tetherfuse.test{_NAME_SEQ[0]}"


@pytest.fixture()
def session_bus():
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        pytest.skip("no session bus available")


@pytest.fixture()
def plugin_process(tmp_path, bus_name):
    netdir = tmp_path / "net"
    netdir.mkdir()
    stub = tmp_path / "stub_backend.py"
    stub.write_text(open(os.path.join(HERE, "stub_backend.py")).read())
    stub.chmod(0o755)
    env = dict(os.environ)
    env["TFVPN_SYSFS_NET"] = str(netdir)
    env["PYTHONPATH"] = SRC
    env["TFVPN_STUB_TUN"] = "tetherfuse0"
    proc = subprocess.Popen(
        [sys.executable, "-m", "tfvpn.plugin", "--session-bus",
         "--bus-name", bus_name, "--debug"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    yield proc, netdir, str(stub), bus_name
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_name(bus, name, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            var = bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "NameHasOwner",
                GLib.Variant("(s)", (name,)), GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE, -1, None)
            if var.unpack()[0]:
                return True
        except GLib.Error:
            pass
        time.sleep(0.1)
    return False


def test_full_activation_cycle(session_bus, plugin_process):
    proc, netdir, stub_path, test_name = plugin_process
    assert _wait_name(session_bus, test_name), \
        "plugin did not acquire the test bus name\n" + (proc.stderr.read() if proc.poll() else "")

    # A real proxy-looking TCP listener so the plugin's readiness probe passes.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    received = []
    loop = GLib.MainLoop()

    def on_signal(conn, sender, path, iface, member, params):
        received.append((member, params.unpack()))
        if member == "StateChanged" and params.unpack()[0] in (4, 6):
            loop.quit()

    session_bus.signal_subscribe(
        test_name, INTERFACE, None, OBJECT_PATH, None,
        Gio.DBusSignalFlags.NONE, on_signal)

    def timeout():
        received.append(("TestTimeout", ()))
        loop.quit()
    GLib.timeout_add_seconds(20, timeout)

    connection = {
        "connection": {"id": GLib.Variant("s", "TetherFuse-test")},
        "vpn": {
            "service-type": GLib.Variant("s", test_name),
            "data": GLib.Variant("a{ss}", {
                "proxy-host": "127.0.0.1",
                "proxy-port": str(port),
                "backend-bin": stub_path,
                "probe-attempts": "5",
                "probe-interval": "1",
            }),
            "secrets": GLib.Variant("a{ss}", {}),
        },
    }

    session_bus.call_sync(
        test_name, OBJECT_PATH, INTERFACE, "ConnectInteractive",
        GLib.Variant("(a{sa{sv}}a{sv})", (connection, {})),
        None, Gio.DBusCallFlags.NONE, -1, None)

    while not received:
        loop.run()
        if received and received[-1][0] == "TestTimeout":
            pytest.fail("timed out waiting for signals; plugin stderr:\n"
                        + proc.stderr.read())

    members = [m for m, _ in received]
    assert "Config" in members, received
    assert "Ip4Config" in members, received

    states = [p[0] for m, p in received if m == "StateChanged"]
    assert 3 in states and 4 in states, states  # STARTING -> STARTED

    config = next(p[0] for m, p in received if m == "Config")
    assert config["tundev"] == "tetherfuse0"
    assert "mtu" in config and config["mtu"] == 1400
    # gateway (external) = proxy address, packed C-style from inet_aton
    assert config["gateway"] == int.from_bytes(socket.inet_aton("127.0.0.1"), "little")

    ip4 = next(p[0] for m, p in received if m == "Ip4Config")
    assert ip4["address"] == int.from_bytes(socket.inet_aton("198.18.0.1"), "little")
    assert ip4["prefix"] == 15
    assert ip4["dns"] == [int.from_bytes(socket.inet_aton("198.18.0.2"), "little")]
    assert ip4["never-default"] is False

    # State property readable like NM reads it
    state = session_bus.call_sync(
        test_name, OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
        GLib.Variant("(ss)", (INTERFACE, "State")), GLib.VariantType("(v)"),
        Gio.DBusCallFlags.NONE, -1, None)
    assert state.unpack()[0] == 4  # STARTED ('v' reply unpacks to plain int)

    # NeedSecrets: NM calls this before activating
    ns = session_bus.call_sync(
        test_name, OBJECT_PATH, INTERFACE, "NeedSecrets",
        GLib.Variant("(a{sa{sv}})", (connection,)), GLib.VariantType("(s)"),
        Gio.DBusCallFlags.NONE, -1, None)
    assert ns.unpack()[0] == ""

    # Disconnect -> STOPPING/STOPPED
    received.clear()
    session_bus.call_sync(
        test_name, OBJECT_PATH, INTERFACE, "Disconnect",
        None, None, Gio.DBusCallFlags.NONE, -1, None)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        GLib.MainContext.default().iteration(True)
        states2 = [p[0] for m, p in received if m == "StateChanged"]
        if 6 in states2:
            break
    states2 = [p[0] for m, p in received if m == "StateChanged"]
    assert 5 in states2 and 6 in states2, received  # STOPPING -> STOPPED

    listener.close()


def test_connect_bad_service_type_returns_dbus_error(session_bus, plugin_process):
    proc, netdir, stub_path, test_name = plugin_process
    assert _wait_name(session_bus, test_name)

    connection = {
        "vpn": {
            "service-type": GLib.Variant("s", "org.not.ours"),
            "data": GLib.Variant("a{ss}", {"proxy-host": "127.0.0.1", "proxy-port": "1"}),
            "secrets": GLib.Variant("a{ss}", {}),
        },
    }
    with pytest.raises(GLib.Error) as exc:
        session_bus.call_sync(
            test_name, OBJECT_PATH, INTERFACE, "Connect",
            GLib.Variant("(a{sa{sv}})", (connection,)), None,
            Gio.DBusCallFlags.NONE, -1, None)
    assert "BadArguments" in exc.value.message
