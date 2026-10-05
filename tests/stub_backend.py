#!/usr/bin/env python3
"""Stub tun2proxy backend for tests.

Creates the fake TUN marker inside $TFVPN_SYSFS_NET (so PluginLogic's
tun_exists() succeeds without any kernel TUN device), then sleeps until
terminated.  Exits with code 42 on USR1 to simulate a backend crash.
"""
import os
import signal
import sys
import time

NET = os.environ.get("TFVPN_SYSFS_NET", "/sys/class/net")
TUN = os.environ.get("TFVPN_STUB_TUN", "tetherfuse0")

os.makedirs(NET, exist_ok=True)
open(os.path.join(NET, TUN), "w").close()
print(f"stub-backend: created {NET}/{TUN}", file=sys.stderr, flush=True)


def die(_, __):
    sys.exit(42)


signal.signal(signal.SIGUSR1, die)
while True:
    time.sleep(3600)
