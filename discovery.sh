#!/usr/bin/env bash
# TetherFuse NM integration — read-only environment discovery.
#
# Prints everything needed to plan the integration.  Changes NOTHING.
# Secrets already stored in NetworkManager profiles are never dumped;
# any environment value that looks like a credential is masked.
#
# Optional:  ./discovery.sh --probe-proxy [host] [port]
#            (defaults: 192.168.49.1 8228)
set -u

PROBE=0
PROXY_HOST=192.168.49.1
PROXY_PORT=8228
if [ "${1:-}" = "--probe-proxy" ]; then
  PROBE=1
  [ $# -ge 2 ] && PROXY_HOST=$2
  [ $# -ge 3 ] && PROXY_PORT=$3
fi

section() { printf '\n===== %s =====\n' "$*"; }
mask() { sed -E 's/((PASS|SECRET|TOKEN|KEY|PSK|PWD)[A-Za-z_]*=).{1,}/\1***MASKED***/Ig'; }

section "OS / arch / desktop"
. /etc/os-release 2>/dev/null && echo "release: $PRETTY_NAME"
echo "arch: $(uname -m)  kernel: $(uname -r)"
command -v gnome-shell >/dev/null && gnome-shell --version
loginctl show-session "$(loginctl 2>/dev/null | awk 'NR==2{print $1}')" -p Type 2>/dev/null | head -1

section "NetworkManager"
NetworkManager --version 2>/dev/null || echo "NetworkManager binary not found"
nmcli -t -f STATE,CONNECTIVITY,WIFI,WWAN general status 2>/dev/null
echo "--- devices ---"
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null

section "Wi-Fi profiles (name | uuid | ssid)"
nmcli -t -f NAME,UUID,TYPE connection show 2>/dev/null | \
  while IFS=: read -r name uuid type; do
    [ "$type" = "802-11-wireless" ] || continue
    ssid=$(nmcli -g 802-11-wireless.ssid connection show "$uuid" 2>/dev/null)
    printf '%s | %s | %s\n' "$name" "$uuid" "$ssid"
  done
echo "--- hotspot candidates (DIRECT-TF-*) ---"
nmcli -t -f NAME,UUID connection show 2>/dev/null | grep -i 'DIRECT-TF' || echo "(none found)"
echo "--- currently active wifi ---"
nmcli -t -f ACTIVE,NAME,UUID,TYPE device wifi list 2>/dev/null | grep '^yes' || true

section "Existing VPN profiles"
nmcli -t -f NAME,UUID,TYPE connection show 2>/dev/null | grep -E ':(vpn|wireguard):' || echo "(none)"

section "Interfaces, addresses, routes"
ip -brief address 2>/dev/null
echo "--- default route(s) ---"
ip route show default 2>/dev/null || echo "(none)"
echo "--- policy rules ---"
ip rule show 2>/dev/null
echo "--- IPv6 default ---"
ip -6 route show default 2>/dev/null || echo "(none)"

section "DNS / systemd-resolved"
systemctl is-active systemd-resolved 2>/dev/null
command -v resolvectl >/dev/null && resolvectl status 2>/dev/null | sed -n '1,30p'
ls -l /etc/resolv.conf 2>/dev/null

section "Firewall"
command -v nft >/dev/null && nft --version
command -v iptables >/dev/null && iptables --version 2>/dev/null
systemctl is-active ufw 2>/dev/null
nft list ruleset >/dev/null 2>&1 && echo "(nft ruleset readable as this user)" \
  || echo "(nft ruleset needs root: sudo nft list ruleset)"
echo "--- interface-specific chains (root needed for full view) ---"
ip link show tailscale0 >/dev/null 2>&1 && echo "tailscale0 present — must stay untouched"

section "Docker"
command -v docker >/dev/null && {
  docker info --format 'server {{.ServerVersion}} driver {{.Driver}}' 2>/dev/null \
    || echo "docker installed but daemon not reachable as this user"
  ip -brief address show docker0 2>/dev/null
} || echo "docker not installed"

section "tun2proxy backend"
command -v tun2proxy && tun2proxy --help 2>&1 | head -3 \
  || echo "tun2proxy not found (installer can install pinned v0.8.4)"

section "Existing proxy configuration (conflict candidates)"
echo "--- shell env (this user) ---"
env | grep -iE '^(https?|all|no)_proxy=' | mask || echo "(none)"
echo "--- shell rc files mentioning proxy ---"
grep -nHiE 'https?_proxy|all_proxy' "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc" \
  /etc/environment /etc/profile.d/*.sh 2>/dev/null | mask || echo "(none)"
echo "--- systemd user session environment ---"
grep -rlE 'https?_proxy|all_proxy' "$HOME/.config/environment.d/" 2>/dev/null | mask || true
systemctl --user show-environment 2>/dev/null | grep -iE '^(https?|all|no)_proxy=' | mask || echo "(none)"
echo "--- GNOME system proxy ---"
gsettings get org.gnome.system.proxy mode 2>/dev/null
gsettings get org.gnome.system.proxy.http host 2>/dev/null
gsettings get org.gnome.system.proxy.http port 2>/dev/null
echo "--- APT proxy ---"
grep -riE '^(Acquire::(http|https)::Proxy|Proxy)' /etc/apt/apt.conf.d/ 2>/dev/null | mask || echo "(none)"
echo "--- systemd system-wide proxy drop-ins ---"
grep -rl 'proxy' /etc/systemd/system.conf.d/ /etc/systemd/user.conf.d/ 2>/dev/null || echo "(none)"
echo "--- NM dispatcher scripts ---"
ls -l /etc/NetworkManager/dispatcher.d/ 2>/dev/null | tail -n +2

section "Privilege / access sanity"
[ "$(id -u)" -eq 0 ] && echo "running as root" || echo "running as $(id -un) (sudo needed for install)"

if [ "$PROBE" -eq 1 ]; then
  section "Proxy probe http://$PROXY_HOST:$PROXY_PORT"
  GW=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
  echo "default gateway: ${GW:-unknown}"
  for url in http://example.com/ https://example.com/; do
    code=$(curl -m 8 -x "http://$PROXY_HOST:$PROXY_PORT" -s -o /dev/null \
      -w '%{http_code}' "$url" 2>/dev/null)
    echo "via proxy  $url -> HTTP ${code:-FAIL}"
  done
  code=$(curl -m 8 --noproxy '*' -s -o /dev/null -w '%{http_code}' http://example.com/ 2>/dev/null)
  echo "direct     http://example.com/ -> HTTP ${code:-FAIL} (000 = hotspot blocks direct)"
fi

printf '\nDiscovery complete. Nothing was modified.\n'
