#!/usr/bin/env bash
# TetherFuse NM VPN integration — idempotent installer.
#
# Usage (root):  sudo ./install.sh [--dry-run] [--proxy HOST:PORT]
#                     [--bind-hotspot UUID] [--install-backend]
#                     [--fail-closed] [--tun2proxy-bin PATH]
#
# Privileged actions performed (each backed up + recorded in a manifest):
#   - install plugin files under /usr/lib/tetherfuse-nm, /usr/libexec,
#     /etc/NetworkManager/VPN, /usr/share/dbus-1/system.d, dispatcher.d
#   - create the "TetherFuse" NM connection profile if it does not exist
#   - with --bind-hotspot: set connection.secondaries on that Wi-Fi profile
#   - systemctl reload NetworkManager (reload, never restart — no network drop)
# With --install-backend: download tun2proxy v0.8.4 (pinned, sha256-verified)
# from github.com/tun2proxy/tun2proxy — nothing is executed before the
# checksum matches.
set -euo pipefail

DRY=0; BIND_UUID=""; PROXY="192.168.49.1:8228"; INSTALL_BACKEND=0
FAIL_CLOSED=false; TUN2PROXY_BIN=""; TUN2PROXY_ZIP=""; DOWNLOAD_PROXY=""
BACKEND_URL=https://github.com/tun2proxy/tun2proxy/releases/download/v0.8.4/tun2proxy-x86_64-unknown-linux-gnu.zip
BACKEND_SHA256=f82c472a97fd686ab0ea5ecea7aead43c272ac66574d9cac02bc36e632df7b84
BACKEND_VER=v0.8.4

while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1 ;;
  --bind-hotspot) BIND_UUID="${2:?UUID required}"; shift ;;
  --proxy) PROXY="${2:?HOST:PORT required}"; shift ;;
  --install-backend) INSTALL_BACKEND=1 ;;
  --fail-closed) FAIL_CLOSED=true ;;
  --tun2proxy-bin) TUN2PROXY_BIN="${2:?path required}"; shift ;;
  --tun2proxy-zip) TUN2PROXY_ZIP="${2:?path required}"; shift ;;
  --download-proxy) DOWNLOAD_PROXY="${2:?http://host:port required}"; shift ;;
  *) echo "unknown option $1"; exit 2 ;;
esac; shift; done

PROXY_HOST="${PROXY%%:*}"; PROXY_PORT="${PROXY##*:}"
ROOT=$(cd "$(dirname "$0")" && pwd)
LIBDIR=/usr/lib/tetherfuse-nm
LIBEXECDIR=/usr/libexec
VPN_DIR=/etc/NetworkManager/VPN
DBUS_DIR=/usr/share/dbus-1/system.d
DISPATCH_DIR=/etc/NetworkManager/dispatcher.d
CONF_DIR=/etc/tetherfuse
STATE_DIR=/var/lib/tetherfuse-nm
MANIFEST=$STATE_DIR/manifest
VPN_ID=TetherFuse
TS=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=/var/backups/tetherfuse-nm/$TS

say() { printf '\n== %s\n' "$*"; }
run() { if [ "$DRY" -eq 1 ]; then echo "DRY: $*"; else eval "$@"; fi; }

# Root's environment has no proxy vars (sudo env_reset), and on a
# proxy-only hotspot plain root curl cannot resolve anything. Find the
# invoking user's proxy: explicit option > inherited env > GNOME manual
# proxy settings of the SUDO_USER.
detect_download_proxy() {
  if [ -n "$DOWNLOAD_PROXY" ]; then printf '%s' "$DOWNLOAD_PROXY"; return 0; fi
  if [ -n "${https_proxy:-}" ]; then printf '%s' "$https_proxy"; return 0; fi
  if [ -n "${http_proxy:-}" ]; then printf '%s' "$http_proxy"; return 0; fi
  if [ -n "${SUDO_USER:-}" ] && command -v gsettings >/dev/null 2>&1; then
    local h p
    h=$(sudo -u "$SUDO_USER" gsettings get org.gnome.system.proxy.http host 2>/dev/null | tr -d "'")
    p=$(sudo -u "$SUDO_USER" gsettings get org.gnome.system.proxy.http port 2>/dev/null | tr -d "'")
    if [ -n "$h" ] && [ "$h" != "''" ] && [ "${p:-0}" -gt 0 ] 2>/dev/null; then
      printf 'http://%s:%s' "$h" "$p"
      return 0
    fi
  fi
  return 1
}

install_backend_zip() {  # $1 = path to already-downloaded zip
  local zip="$1" tmp bin
  [ -f "$zip" ] || { echo "FATAL: $zip not found"; exit 1; }
  echo "$BACKEND_SHA256  $zip" | sha256sum -c - || {
    echo "FATAL: checksum mismatch for $zip — refusing to install"; exit 1; }
  tmp=$(mktemp -d)
  unzip -o -q "$zip" -d "$tmp"
  # v0.8.4 ships the binary as "tun2proxy-bin" next to udpgw-server/libtun2proxy.so
  bin=$(find "$tmp" -maxdepth 2 -type f \( -name 'tun2proxy-bin' -o -name 'tun2proxy' \) | head -1)
  [ -n "$bin" ] || { echo "FATAL: no tun2proxy binary inside $zip"; ls -la "$tmp"; rm -rf "$tmp"; exit 1; }
  install -m 0755 "$bin" /usr/bin/tun2proxy
  rm -rf "$tmp"
  sha256sum /usr/bin/tun2proxy >> "$MANIFEST.tmp" 2>/dev/null || true
  echo "installed /usr/bin/tun2proxy ($BACKEND_VER, sha256-verified)"
}

install_file() {  # src dst mode
  local src=$1 dst=$2 mode=$3
  if [ -e "$dst" ] && cmp -s "$src" "$dst"; then
    echo "  unchanged: $dst"
  else
    if [ -e "$dst" ]; then
      run "mkdir -p '$BACKUP_DIR' && cp -a '$dst' '$BACKUP_DIR/'"
      echo "  backed up existing: $dst -> $BACKUP_DIR"
    fi
    run "install -D -m $mode '$src' '$dst'"
    echo "  installed: $dst"
  fi
  [ "$DRY" -eq 0 ] && sha256sum "$dst" >> "$MANIFEST.tmp" 2>/dev/null || true
}

# ---------------------------------------------------------------- preflight
say "Preflight"
if [ "$(id -u)" -ne 0 ] && [ "$DRY" -eq 0 ]; then
  echo "must run as root (sudo ./install.sh)"; exit 1
fi
echo "os: $(. /etc/os-release && echo "$PRETTY_NAME")  arch: $(uname -m)"
NM_VER=$(NetworkManager --version 2>/dev/null | head -1 || true)
[ -n "$NM_VER" ] || { echo "FATAL: NetworkManager not found"; exit 1; }
echo "NetworkManager: $NM_VER"
python3 -c 'import gi' 2>/dev/null || {
  echo "FATAL: python3-gi missing. Install with: sudo apt install python3-gi"; exit 1; }
echo "python3-gi: ok ($(python3 -c 'import gi; print(gi.__version__)'))"
command -v nft >/dev/null || echo "WARN: nft not found — fail-closed mode unavailable"

if [ -z "$TUN2PROXY_BIN" ]; then
  if command -v tun2proxy >/dev/null 2>&1; then
    TUN2PROXY_BIN=$(command -v tun2proxy)
    echo "tun2proxy: found at $TUN2PROXY_BIN ($(tun2proxy --version 2>/dev/null | head -1 || version-unknown))"
  elif [ -n "$TUN2PROXY_ZIP" ]; then
    say "Installing backend from local zip $TUN2PROXY_ZIP"
    if [ "$DRY" -eq 0 ]; then install_backend_zip "$TUN2PROXY_ZIP"; fi
    TUN2PROXY_BIN=/usr/bin/tun2proxy
  elif [ "$INSTALL_BACKEND" -eq 1 ]; then
    [ "$(uname -m)" = "x86_64" ] || { echo "FATAL: pinned binary is x86_64-only on $(uname -m); use --tun2proxy-bin or cargo"; exit 1; }
    command -v curl >/dev/null && command -v unzip >/dev/null || {
      echo "FATAL: curl+unzip required for --install-backend"; exit 1; }
    say "Installing backend tun2proxy $BACKEND_VER (pinned, sha256-verified)"
    PX=$(detect_download_proxy || true)
    if [ -n "$PX" ]; then
      echo "downloading via proxy: $PX"
    else
      echo "NOTE: no proxy detected for root; if download fails, download the zip in your"
      echo "      normal session and use: --tun2proxy-zip /path/to/tun2proxy-x86_64-unknown-linux-gnu.zip"
    fi
    TMP=$(mktemp -d)
    if [ "$DRY" -eq 0 ]; then
      CURL_ARGS=(-fsSL --retry 3 --connect-timeout 15 --max-time 300 -o "$TMP/tun2proxy.zip")
      [ -n "$PX" ] && CURL_ARGS+=(--proxy "$PX")
      curl "${CURL_ARGS[@]}" "$BACKEND_URL"
      install_backend_zip "$TMP/tun2proxy.zip"
    else
      echo "DRY: curl ${CURL_ARGS[*]:-} $BACKEND_URL + verify + install"
    fi
    rm -rf "$TMP"
    TUN2PROXY_BIN=/usr/bin/tun2proxy
  else
    echo "WARN: tun2proxy not found."
    echo "      option A: sudo ./install.sh --install-backend   (pinned $BACKEND_VER, sha256-verified;"
    echo "                 auto-uses your GNOME proxy settings on proxy-only hotspots)"
    echo "      option B: download the zip in your session (works via your proxy env), then:"
    echo "                 sudo ./install.sh --tun2proxy-zip ~/tun2proxy-x86_64-unknown-linux-gnu.zip"
    echo "      option C: cargo install tun2proxy --version $BACKEND_VER --locked"
    echo "      option D: --tun2proxy-bin /path/to/tun2proxy"
    echo "      The plugin will fail cleanly at activation until a backend is present."
  fi
else
  [ -x "$TUN2PROXY_BIN" ] || { echo "FATAL: --tun2proxy-bin $TUN2PROXY_BIN not executable"; exit 1; }
  echo "tun2proxy: using provided $TUN2PROXY_BIN"
fi

if [ "$DRY" -eq 0 ]; then
  mkdir -p "$STATE_DIR" "$CONF_DIR"
  : > "$MANIFEST.tmp"
fi

# ---------------------------------------------------------------- files
say "Installing files (root-owned, 0755/0644)"
run "mkdir -p '$LIBDIR/tfvpn'"
install_file "$ROOT/src/tfvpn/__init__.py"  "$LIBDIR/tfvpn/__init__.py"  0644
install_file "$ROOT/src/tfvpn/config.py"    "$LIBDIR/tfvpn/config.py"    0644
install_file "$ROOT/src/tfvpn/backend.py"   "$LIBDIR/tfvpn/backend.py"   0644
install_file "$ROOT/src/tfvpn/firewall.py"  "$LIBDIR/tfvpn/firewall.py"  0644
install_file "$ROOT/src/tfvpn/logic.py"     "$LIBDIR/tfvpn/logic.py"     0644
install_file "$ROOT/src/tfvpn/plugin.py"    "$LIBDIR/tfvpn/plugin.py"    0644
install_file "$ROOT/tools/tfvpn-plugin"     "$LIBEXECDIR/tfvpn-plugin"   0755
install_file "$ROOT/data/tetherfuse.name"   "$VPN_DIR/tetherfuse.name"   0644
install_file "$ROOT/data/org.freedesktop.NetworkManager.tetherfuse.conf" \
                                              "$DBUS_DIR/org.freedesktop.NetworkManager.tetherfuse.conf" 0644
install_file "$ROOT/data/70-tetherfuse"     "$DISPATCH_DIR/70-tetherfuse" 0755
run "printf '%s\n' '$VPN_ID' > '$CONF_DIR/vpn-id'"

# ---------------------------------------------------------------- reload FIRST
say "Reloading NetworkManager (no service restart, no network interruption)"
# NM must (re)scan /etc/NetworkManager/VPN before a profile referencing the
# new service-type can be created.
run "systemctl reload NetworkManager"
[ "$DRY" -eq 0 ] && sleep 2

# ---------------------------------------------------------------- profile
say "NetworkManager profile '$VPN_ID'"
PROFILE_FILE=/etc/NetworkManager/system-connections/$VPN_ID.nmconnection
if nmcli connection show id "$VPN_ID" >/dev/null 2>&1; then
  echo "profile already exists in NM — leaving it untouched"
elif [ -e "$PROFILE_FILE" ]; then
  echo "profile file exists on disk but NM does not list it; reloading"
  run "nmcli connection reload"
else
  # Write the keyfile directly, then tell NM to (re)load it. This avoids
  # nmcli-add races (NM persists the keyfile asynchronously after AddConnection
  # returns) and any vpn.data parsing quirks.
  if [ "$DRY" -eq 0 ]; then
    cat > "$PROFILE_FILE" <<EOF
[connection]
id=$VPN_ID
type=vpn
autoconnect=false

[vpn]
service-type=org.freedesktop.NetworkManager.tetherfuse
proxy-host=$PROXY_HOST
proxy-port=$PROXY_PORT
dns=virtual
fail-closed=$FAIL_CLOSED

[ipv4]
method=auto

[ipv6]
method=disabled
EOF
    chmod 600 "$PROFILE_FILE"
    sha256sum "$PROFILE_FILE" >> "$MANIFEST.tmp" 2>/dev/null || true
    nmcli connection reload
    sleep 1
    if nmcli connection show id "$VPN_ID" >/dev/null 2>&1; then
      echo "wrote $PROFILE_FILE and NM loaded it (proxy $PROXY_HOST:$PROXY_PORT)"
      echo "$VPN_ID.nmconnection" > "$STATE_DIR/owned-profile"
    else
      echo "WARN: NM did not load $PROFILE_FILE — check 'journalctl -u NetworkManager | grep -i keyfile'"
    fi
  else
    echo "DRY: write $PROFILE_FILE (proxy $PROXY_HOST:$PROXY_PORT) + nmcli connection reload"
  fi
fi

# ---------------------------------------------------------------- hotspot
if [ -n "$BIND_UUID" ]; then
  say "Binding hotspot $BIND_UUID (connection.secondaries + dispatcher id)"
  VPN_UUID=$(nmcli -g connection.uuid connection show id "$VPN_ID" 2>/dev/null || true)
  if [ -n "$VPN_UUID" ] && [ "$DRY" -eq 0 ]; then
    nmcli connection modify "$BIND_UUID" +connection.secondaries "$VPN_UUID"
    printf '%s\n' "$BIND_UUID" > "$CONF_DIR/hotspot-uuid"
    echo "hotspot profile now auto-activates '$VPN_ID' on connect"
    echo "manual VPN-off stays off until the hotspot reconnects (NM semantics)"
  else
    echo "DRY: nmcli connection modify $BIND_UUID +connection.secondaries <uuid>"
  fi
else
  say "Hotspot binding skipped (--bind-hotspot <UUID> to enable)"
  nmcli -t -f NAME,UUID connection show 2>/dev/null | grep -i 'DIRECT-TF' || true
fi

# ---------------------------------------------------------------- verify
if [ "$DRY" -eq 0 ]; then
  if nmcli connection show id "$VPN_ID" >/dev/null 2>&1; then
    echo "NM sees profile '$VPN_ID' — plugin registered."
  else
    echo "WARN: NM has not picked up the VPN plugin yet."
    echo "      Check: journalctl -u NetworkManager --since '-2 min' | grep -i vpn"
    echo "      A logout/login or 'sudo systemctl restart NetworkManager' (brief network drop) forces a rescan."
  fi
fi
[ "$DRY" -eq 0 ] && { sort -u "$MANIFEST.tmp" > "$MANIFEST"; rm -f "$MANIFEST.tmp"; }

say "Done"
cat <<EOF

Next steps:
  1. Verify the plugin service file:  cat $VPN_DIR/tetherfuse.name
  2. Activate manually once:          nmcli connection up id $VPN_ID
  3. Watch state:                     nmcli connection show id $VPN_ID
     Logs:                            journalctl -u NetworkManager -f | grep tetherfuse
                                      journalctl -u NetworkManager -f | grep -i vpn
  4. GNOME: the connection appears under Settings > Network > VPN and in
     Quick Settings once NM has activated it at least once.
  5. Hotspot auto-activation: rerun with --bind-hotspot <UUID> (see
     ./discovery.sh for the DIRECT-TF-* profile UUIDs).
EOF
