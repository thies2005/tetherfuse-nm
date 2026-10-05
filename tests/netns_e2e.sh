#!/usr/bin/env bash
# TetherFuse end-to-end test inside a network namespace (ROOT ONLY).
#
# Exercises the real data path with no phone required:
#   curl -> tun0 (default route) -> tun2proxy -> mock CONNECT proxy (host side)
# plus the virtual-DNS path (resolv.conf -> 198.18.0.1 via tun -> tun2proxy).
#
# Requirements: root, tun2proxy binary, python3.
# Usage: sudo tests/netns_e2e.sh [--tun2proxy /path/to/tun2proxy]
set -euo pipefail

TUN2PROXY=$(command -v tun2proxy || true)
[ "${1:-}" = "--tun2proxy" ] && TUN2PROXY="${2:?path required}"
[ -n "$TUN2PROXY" ] || { echo "FATAL: tun2proxy binary not found (pass --tun2proxy)"; exit 1; }

[ "$(id -u)" -eq 0 ] || { echo "FATAL: must run as root (sudo)"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)

NS=tfclient
VETH_HOST=tfvhost0
VETH_NS=tfvclient0
PROXY_IP=192.168.100.1
PROXY_PORT=8888
MARKER="TETHERFUSE-MOCK-PROXY-OK"
RC=0

cleanup() {
  ip netns del "$NS" 2>/dev/null || true
  [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "== building namespace + mock proxy"
ip netns add "$NS"
ip link add "$VETH_HOST" type veth peer name "$VETH_NS"
ip link set "$VETH_NS" netns "$NS"
ip addr add ${PROXY_IP}/24 dev "$VETH_HOST"
ip link set "$VETH_HOST" up
ip netns exec "$NS" ip addr add 192.168.100.2/24 dev "$VETH_NS"
ip netns exec "$NS" ip link set lo up
ip netns exec "$NS" ip link set "$VETH_NS" up
ip netns exec "$NS" ip route add ${PROXY_IP}/32 dev "$VETH_NS"

python3 "$HERE/mock_connect_proxy.py" "$PROXY_PORT" --canned &
MOCK_PID=$!
sleep 0.5

echo "== starting tun2proxy inside the namespace (virtual DNS, no --setup)"
ip netns exec "$NS" "$TUN2PROXY" \
  --tun tun0 --proxy "http://${PROXY_IP}:${PROXY_PORT}" \
  --dns virtual --exit-on-fatal-error &
sleep 1
ip netns exec "$NS" ip addr add 198.18.0.1/15 dev tun0
ip netns exec "$NS" ip link set tun0 up
ip netns exec "$NS" ip route add default dev tun0 metric 50
mkdir -p "/etc/netns/$NS"
echo "nameserver 198.18.0.1" > "/etc/netns/$NS/resolv.conf"

echo "== check 1: HTTP via tunnel, DNS via virtual resolver"
OUT=$(ip netns exec "$NS" curl --noproxy '*' -m 15 -s http://e2e-test.invalid/ || true)
if echo "$OUT" | grep -q "$MARKER"; then echo "PASS: http via tun0 + virtual DNS"; else
  echo "FAIL: body was '$OUT'"; RC=1; fi

echo "== check 2: proxy reached from namespace (bypass not needed)"
ip netns exec "$NS" python3 -c "
import socket
s = socket.create_connection(('$PROXY_IP', $PROXY_PORT), timeout=5)
print('PASS: proxy TCP reachable (stays outside tunnel)')" || { echo FAIL; RC=1; }

echo "== check 3: no routing loop — host-side veth counters increase only"
RX1=$(cat /sys/class/net/$VETH_HOST/statistics/rx_packets)
ip netns exec "$NS" curl --noproxy '*' -m 15 -s -o /dev/null http://second-test.invalid/ || true
RX2=$(cat /sys/class/net/$VETH_HOST/statistics/rx_packets)
if [ "$RX2" -gt "$RX1" ]; then echo "PASS: forward path alive (rx $RX1 -> $RX2)"; else
  echo "FAIL: no host-side traffic (rx $RX1 -> $RX2)"; RC=1; fi

rm -rf "/etc/netns/$NS"
echo
[ "$RC" -eq 0 ] && echo "E2E: ALL CHECKS PASSED" || echo "E2E: FAILURES PRESENT"
exit $RC
