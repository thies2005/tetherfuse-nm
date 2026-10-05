"""NetworkManager VPN service plugin for TetherFuse (D-Bus service).

Implements org.freedesktop.NetworkManager.VPN.Plugin exactly as specified by
NetworkManager's introspection/org.freedesktop.NetworkManager.VPN.Plugin.xml
(verified against NM 1.52.1 source; the interface is stable since ~1.16 and
the local daemon is 1.54.3).  NM launches this program via the ``program=``
line of /etc/NetworkManager/VPN/tetherfuse.name and speaks this interface on
the SYSTEM bus.

This is a genuine NM VPN plugin: GNOME's native VPN controls drive the same
connection, and the connection state NM tracks is derived from the signals we
emit — never merely from the fact that this process started.
"""
from __future__ import annotations

import argparse
import signal
import sys

try:
    from gi.repository import GLib, Gio
except ImportError:  # pragma: no cover - environment guard for non-GNOME hosts
    print("tetherfuse-plugin: python3-gi (PyGObject) is required", file=sys.stderr)
    raise

from .backend import BackendProcess
from .config import ConfigError
from .firewall import Firewall
from .logic import BusyConnection, PluginLogic

SERVICE_TYPE = "org.freedesktop.NetworkManager.tetherfuse"
OBJECT_PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
INTERFACE = "org.freedesktop.NetworkManager.VPN.Plugin"
NM_DBUS_ERROR = "org.freedesktop.NetworkManager.VPN.Error"
NM_SERVICE = "org.freedesktop.NetworkManager"

# Verbatim from NM's introspection XML (C-symbol annotations stripped; method
# arg names/types/directions, signals, and the State property are unchanged).
IFACE_XML = f"""
<node>
  <interface name='{INTERFACE}'>
    <method name='Connect'>
      <arg name='connection' type='a{{sa{{sv}}}}' direction='in'/>
    </method>
    <method name='ConnectInteractive'>
      <arg name='connection' type='a{{sa{{sv}}}}' direction='in'/>
      <arg name='details' type='a{{sv}}' direction='in'/>
    </method>
    <method name='NeedSecrets'>
      <arg name='settings' type='a{{sa{{sv}}}}' direction='in'/>
      <arg name='setting_name' type='s' direction='out'/>
    </method>
    <method name='Disconnect'>
    </method>
    <method name='SetConfig'>
      <arg name='config' type='a{{sv}}' direction='in'/>
    </method>
    <method name='SetIp4Config'>
      <arg name='config' type='a{{sv}}' direction='in'/>
    </method>
    <method name='SetIp6Config'>
      <arg name='config' type='a{{sv}}' direction='in'/>
    </method>
    <method name='SetFailure'>
      <arg name='reason' type='s' direction='in'/>
    </method>
    <method name='NewSecrets'>
      <arg name='connection' type='a{{sa{{sv}}}}' direction='in'/>
    </method>
    <property name='State' type='u' access='read'/>
    <signal name='StateChanged'>
      <arg name='state' type='u'/>
    </signal>
    <signal name='SecretsRequired'>
      <arg name='message' type='s'/>
      <arg name='secrets' type='as'/>
    </signal>
    <signal name='Config'>
      <arg name='config' type='a{{sv}}'/>
    </signal>
    <signal name='Ip4Config'>
      <arg name='ip4config' type='a{{sv}}'/>
    </signal>
    <signal name='Ip6Config'>
      <arg name='ip6config' type='a{{sv}}'/>
    </signal>
    <signal name='LoginBanner'>
      <arg name='banner' type='s'/>
    </signal>
    <signal name='Failure'>
      <arg name='reason' type='u'/>
    </signal>
  </interface>
</node>
"""

_DICT_SIGNALS = {"Config", "Ip4Config", "Ip6Config"}


class VpnPluginService:
    def __init__(self, bus: "Gio.DBusConnection", bus_name: str, debug: bool,
                 firewall=None):
        self.bus = bus
        self.bus_name = bus_name
        self.debug = debug
        self.interface = Gio.DBusNodeInfo.new_for_xml(IFACE_XML).interfaces[0]
        self.logic = PluginLogic(
            emit=self._emit,
            schedule=self._schedule,
            spawn_backend=self._spawn_backend,
            firewall=firewall if firewall is not None else Firewall(),
            service_type=bus_name,
            debug=debug,
        )
        bus.register_object(
            OBJECT_PATH, self.interface,
            self._method_call, self._get_property, None,
        )

    # ------------------------------------------------------------------
    def _emit(self, name: str, body: dict) -> None:
        if name in _DICT_SIGNALS:
            payload = {k: GLib.Variant(t, v) for k, (t, v) in body.items()}
            params = GLib.Variant("(a{sv})", (payload,))
        elif name == "StateChanged":
            params = GLib.Variant("(u)", (body["state"][1],))
        elif name == "Failure":
            params = GLib.Variant("(u)", (body["reason"][1],))
        else:
            raise ValueError(f"unmapped signal {name}")
        self.bus.emit_signal(None, OBJECT_PATH, INTERFACE, name, params)

    @staticmethod
    def _schedule(delay_s: float, fn):
        def wrapper():
            fn()
            return False  # one-shot
        return GLib.timeout_add(int(max(delay_s, 0.001) * 1000), wrapper)

    def _spawn_backend(self, argv):
        proc = BackendProcess(argv)
        proc.spawn()
        GLib.child_watch_add(proc.proc.pid,
                             lambda pid, status: self.logic.child_exited(status))
        return proc

    # ------------------------------------------------------------------
    def _get_property(self, connection, sender, path, iface, prop):
        if prop == "State":
            return GLib.Variant("u", self.logic.state)
        return None

    def _method_call(self, connection, sender, path, iface, name, parameters, invocation):
        try:
            if name in ("Connect", "ConnectInteractive"):
                self.logic.connect(_unpack_connection(parameters.get_child_value(0)))
                invocation.return_value()
            elif name == "NeedSecrets":
                invocation.return_value(GLib.Variant("(s)", ("",)))
            elif name == "Disconnect":
                self.logic.disconnect()
                invocation.return_value()
            elif name == "NewSecrets":
                self.logic.new_secrets(_unpack_connection(parameters.get_child_value(0)))
                invocation.return_value()
            elif name in ("SetConfig", "SetIp4Config", "SetIp6Config"):
                sig = {"SetConfig": "Config", "SetIp4Config": "Ip4Config",
                       "SetIp6Config": "Ip6Config"}[name]
                dv = parameters.get_child_value(0)
                out = {}
                for i in range(dv.n_children()):
                    entry = dv.get_child_value(i)
                    out[entry.get_child_value(0).get_string()] = entry.get_child_value(1)
                connection.emit_signal(None, OBJECT_PATH, INTERFACE, sig,
                                       GLib.Variant("(a{sv})", out))
                invocation.return_value()
            elif name == "SetFailure":
                self.logic.set_failure(parameters.get_child_value(0).get_string())
                invocation.return_value()
            else:  # pragma: no cover
                invocation.return_dbus_error(
                    f"{NM_DBUS_ERROR}.UnknownMethod", f"unknown method {name}")
        except ConfigError as e:
            invocation.return_dbus_error(f"{NM_DBUS_ERROR}.BadArguments", str(e))
        except BusyConnection as e:
            err = "StartingInProgress" if self.logic.state == 3 else "AlreadyStarted"
            invocation.return_dbus_error(f"{NM_DBUS_ERROR}.{err}", str(e))


def _unpack_connection(variant: "GLib.Variant") -> dict:
    """a{sa{sv}} -> plain python, with inner values deep-unpacked."""
    out = {}
    for i in range(variant.n_children()):
        section = variant.get_child_value(i)
        name = section.get_child_value(0).get_string()
        inner = section.get_child_value(1)
        d = {}
        for j in range(inner.n_children()):
            entry = inner.get_child_value(j)
            key = entry.get_child_value(0).get_string()
            val = entry.get_child_value(1)
            d[key] = val.unpack()
        out[name] = d
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TetherFuse NM VPN plugin service")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--bus-name", default=SERVICE_TYPE,
                    help="D-Bus name to own (must equal the NM vpn service-type)")
    ap.add_argument("--session-bus", action="store_true",
                    help="own the name on the session bus (testing only)")
    args = ap.parse_args(argv)

    loop = GLib.MainLoop()
    bus_type = Gio.BusType.SESSION if args.session_bus else Gio.BusType.SYSTEM
    service_holder: list = []

    def on_bus_acquired(conn, name):
        service_holder.append(VpnPluginService(conn, name, args.debug))
        print(f"tetherfuse-plugin: acquired {name} at {OBJECT_PATH}",
              file=sys.stderr, flush=True)

    def on_name_lost(_conn, name):
        print(f"tetherfuse-plugin: failed to acquire {name}; exiting",
              file=sys.stderr, flush=True)
        loop.quit()

    Gio.bus_own_name(bus_type, args.bus_name, Gio.BusNameOwnerFlags.NONE,
                     on_bus_acquired, None, on_name_lost)

    # If NetworkManager disappears we are orphaned: exit (NM will respawn us
    # on the next activation).  Meaningful on the system bus only — the
    # session-bus mode exists for tests, where NM is never present.
    if not args.session_bus:
        Gio.bus_watch_name(bus_type, NM_SERVICE, Gio.BusNameWatcherFlags.NONE,
                           None, lambda *a: loop.quit())

    def _term(*_):
        if service_holder:
            service_holder[0].logic.shutdown()
        loop.quit()
        return False

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _term)

    loop.run()
    if service_holder:
        service_holder[0].logic.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
