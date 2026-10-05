#!/usr/bin/env bash
# TetherFuse NM VPN integration — interactive setup wizard.
#
# Walks you through:
#   1. choosing the TetherFuse hotspot's saved Wi-Fi profile
#   2. entering the phone proxy host:port (default: TetherFuse's 192.168.49.1:8228)
#   3. optional fail-closed leak prevention and backend installation
# then runs install.sh with those choices.
#
# Prompts run as your normal user; sudo is only invoked for the final
# install step. Supports --dry-run to preview without touching anything.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

prompt() {  # var default question
  local __var=$1 __def=$2 __q=$3 __ans
  read -r -p "$__q [$__def]: " __ans || true
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

confirm() {  # question default(y/n) -> rc 0 on yes
  local q=$1 def=${2:-y} ans
  read -r -p "$q ($([ "$def" = y ] && echo Y/n || echo y/N)): " ans || true
  ans=${ans:-$def}
  [[ "$ans" =~ ^[Yy] ]]
}

echo "=========================================================="
echo " TetherFuse NetworkManager VPN — setup"
echo "=========================================================="
echo "Turns the TetherFuse phone hotspot's HTTP CONNECT proxy into a"
echo "native NetworkManager VPN connection (GNOME VPN toggle included)."

command -v nmcli >/dev/null || { echo "FATAL: nmcli not found"; exit 1; }

# ---------------------------------------------------------------- hotspot
echo
echo "Saved Wi-Fi profiles:"
mapfile -t WIFI_ENTRIES < <(nmcli -t -f NAME,UUID,TYPE connection show 2>/dev/null \
  | awk -F: '$3 == "802-11-wireless"')
if [ "${#WIFI_ENTRIES[@]}" -eq 0 ]; then
  echo "No saved Wi-Fi profiles found. Connect to the hotspot once, then rerun."
  exit 1
fi
WIFI_NAMES=() ; WIFI_UUIDS=()
for e in "${WIFI_ENTRIES[@]}"; do
  WIFI_NAMES+=("${e%%:*}")
  WIFI_UUIDS+=("$(printf '%s' "$e" | awk -F: '{print $(NF-1)}')")
done
ACTIVE=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
  | awk -F: '$2 != "" {print $1; exit}')
DEF_IDX=1
for i in "${!WIFI_NAMES[@]}"; do
  [ "${WIFI_NAMES[$i]}" = "$ACTIVE" ] && DEF_IDX=$((i + 1))
done
for i in "${!WIFI_NAMES[@]}"; do
  printf '  %2d) %s%s\n' "$((i + 1))" "${WIFI_NAMES[$i]}" \
    "$([ $((i + 1)) -eq "$DEF_IDX" ] && echo '   <- currently connected')"
done
while :; do
  read -r -p "Number of the TetherFuse hotspot profile [$DEF_IDX]: " idx || true
  idx=${idx:-$DEF_IDX}
  if [[ "$idx" =~ ^[0-9]+$ ]] && [ "$idx" -ge 1 ] && [ "$idx" -le "${#WIFI_NAMES[@]}" ]; then
    break
  fi
  echo "Invalid selection, try again."
done
HOTSPOT_UUID=${WIFI_UUIDS[$((idx - 1))]}
HOTSPOT_NAME=${WIFI_NAMES[$((idx - 1))]}

# ---------------------------------------------------------------- proxy
echo
GW=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
[ -n "$GW" ] && echo "Current default gateway: $GW (the phone, when on the hotspot)"
prompt PROXY "192.168.49.1:8228" "Phone proxy host:port (TetherFuse default)"
PROXY_HOST=${PROXY%%:*}
PROXY_PORT=${PROXY##*:}
if ! [[ "$PROXY_PORT" =~ ^[0-9]+$ ]] || [ "$PROXY_PORT" -lt 1 ] || [ "$PROXY_PORT" -gt 65535 ]; then
  echo "FATAL: invalid port '$PROXY_PORT'"; exit 1
fi

echo
echo "Probing proxy http://$PROXY_HOST:$PROXY_PORT ..."
CODE=$(curl -m 6 -x "http://$PROXY_HOST:$PROXY_PORT" -s -o /dev/null \
  -w '%{http_code}' http://example.com/ 2>/dev/null || true)
if [ "$CODE" = "200" ]; then
  echo "  OK — proxy reachable, CONNECT works."
else
  echo "  No answer (HTTP ${CODE:-none}). If you are not on the hotspot yet, that is"
  echo "  fine: installation proceeds and activation retries when you connect."
  confirm "Continue anyway?" y || exit 0
fi

# ---------------------------------------------------------------- options
echo
FAIL=()
confirm "Enable fail-closed leak prevention while connected?" n && FAIL=("--fail-closed")
BACKEND=()
if command -v tun2proxy >/dev/null 2>&1; then
  echo "tun2proxy already installed ($(command -v tun2proxy))."
else
  confirm "Download and install the tun2proxy backend now? (pinned v0.8.4, sha256-verified)" y \
    && BACKEND=("--install-backend")
fi

ARGS=(--proxy "$PROXY_HOST:$PROXY_PORT" --bind-hotspot "$HOTSPOT_UUID" ${FAIL[@]+"${FAIL[@]}"} ${BACKEND[@]+"${BACKEND[@]}"})
[ "$DRY" -eq 1 ] && ARGS+=(--dry-run)

echo
echo "Hotspot : $HOTSPOT_NAME ($HOTSPOT_UUID)"
echo "Proxy   : http://$PROXY_HOST:$PROXY_PORT"
echo "Command : ${SUDO-} ./install.sh ${ARGS[*]}"
echo

if [ "$DRY" -eq 1 ]; then
  echo "Dry run — invoking install.sh without sudo (it previews only):"
  exec "$ROOT/install.sh" "${ARGS[@]}"
fi
echo "Running installer as root (password may be asked)..."
exec sudo "$ROOT/install.sh" "${ARGS[@]}"
