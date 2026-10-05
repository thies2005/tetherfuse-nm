#!/usr/bin/env bash
# TetherFuse NM integration — uninstaller.
#
# Removes ONLY what this integration owns:
#  - files recorded in the install manifest whose sha256 still matches
#    (files the user modified afterwards are kept, with a warning)
#  - the "TetherFuse" NM profile (with --remove-profile; only if its
#    service-type is ours)
#  - connection.secondaries entries pointing at our profile UUID
#  - the nftables table "inet tetherfuse_vpn" (best effort)
# Wi-Fi profiles, other VPN profiles, Docker/UFW rules are never touched.
set -euo pipefail

REMOVE_PROFILE=0
[ "${1:-}" = "--remove-profile" ] && REMOVE_PROFILE=1

STATE_DIR=/var/lib/tetherfuse-nm
MANIFEST=$STATE_DIR/manifest
CONF_DIR=/etc/tetherfuse
VPN_ID=TetherFuse
SERVICE_TYPE=org.freedesktop.NetworkManager.tetherfuse

[ "$(id -u)" -eq 0 ] || { echo "must run as root (sudo ./uninstall.sh)"; exit 1; }

echo "== deactivate if running"
nmcli connection down id "$VPN_ID" >/dev/null 2>&1 || true

echo "== remove hotspot binding (secondaries) if present"
VPN_UUID=$(nmcli -g connection.uuid connection show id "$VPN_ID" 2>/dev/null || true)
if [ -n "$VPN_UUID" ]; then
  while IFS=: read -r name uuid type; do
    [ "$type" = "802-11-wireless" ] || continue
    cur=$(nmcli -g connection.secondaries connection show uuid "$uuid" 2>/dev/null || true)
    case ",$cur," in
      *",$VPN_UUID,"*)
        nmcli connection modify uuid "$uuid" -connection.secondaries "$VPN_UUID" \
          && echo "  unbound $name";;
    esac
  done < <(nmcli -t -f NAME,UUID,TYPE connection show 2>/dev/null)
fi

if [ "$REMOVE_PROFILE" -eq 1 ] && [ -n "$VPN_UUID" ]; then
  st=$(nmcli -g vpn.service-type connection show id "$VPN_ID" 2>/dev/null || true)
  if [ "$st" = "$SERVICE_TYPE" ]; then
    nmcli connection delete id "$VPN_ID" && echo "== deleted profile '$VPN_ID'"
  else
    echo "== refusing to delete '$VPN_ID': service-type is '$st', not ours"
  fi
fi

echo "== remove nftables table (ours only)"
nft delete table inet tetherfuse_vpn 2>/dev/null && echo "  table removed" || echo "  no table present"

echo "== remove installed files (hash-verified)"
if [ -f "$MANIFEST" ]; then
  while read -r sha path; do
    [ -e "$path" ] || continue
    cur=$(sha256sum "$path" 2>/dev/null | awk '{print $1}' || true)
    if [ "$cur" = "$sha" ]; then
      rm -f "$path" && echo "  removed $path"
    else
      echo "  KEEP (modified since install): $path"
    fi
  done < "$MANIFEST"
else
  echo "  no manifest found — removing known paths best-effort"
  rm -f /usr/libexec/tfvpn-plugin /etc/NetworkManager/VPN/tetherfuse.name \
        /usr/share/dbus-1/system.d/org.freedesktop.NetworkManager.tetherfuse.conf \
        /etc/NetworkManager/dispatcher.d/70-tetherfuse
  rm -rf /usr/lib/tetherfuse-nm
fi
rmdir /usr/lib/tetherfuse-nm/tfvpn /usr/lib/tetherfuse-nm 2>/dev/null || true
rm -rf "$CONF_DIR" "$STATE_DIR"

echo "== reload NetworkManager"
systemctl reload NetworkManager || true

echo "Uninstall complete. tun2proxy binary (if installed) left at $(command -v tun2proxy || echo '/usr/bin/tun2proxy') —"
echo "remove with: sudo rm -f /usr/bin/tun2proxy"
