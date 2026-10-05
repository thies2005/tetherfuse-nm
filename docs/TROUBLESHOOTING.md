# Troubleshooting cookbook

## Plugin not picked up by NM

```bash
cat /etc/NetworkManager/VPN/tetherfuse.name        # service + program paths
ls -l /usr/libexec/tfvpn-plugin                    # must be executable, root
journalctl -u NetworkManager --since -10min | grep -iE 'vpn|tetherfuse'
sudo systemctl reload NetworkManager               # rescan VPN dir (no drop)
nmcli connection show id TetherFuse                # profile loadable?
```
If NM still doesn't list the service type, a logout/login (or a brief
`systemctl restart NetworkManager`) forces a full rescan.

## Activation fails

```bash
journalctl -u NetworkManager --since -5min | grep tetherfuse-plugin  # FSM decisions
journalctl -u NetworkManager --since -5min | grep -i vpn             # NM-side view
tun2proxy --version                                # backend present?
nmcli -g vpn.data connection show id TetherFuse    # effective settings
```
(The plugin is a child of the NetworkManager service, so its stderr lands in
NM's unit journal — `journalctl -t tetherfuse-plugin` shows nothing.)
Failure codes on the NM `Failure` signal: `0` login/proxy-auth,
`1` connect failed (backend exit, proxy unreachable, timeout), `2` bad IP
config. The plugin log line names the exact cause.

Common causes:
* proxy not ready yet after Wi-Fi join → plugin retries (default 8×2 s);
  increase `probe-attempts` if the phone is slow.
* `backend exited during activation (rc=…)` → run tun2proxy by hand:
  `sudo tun2proxy --tun tetherfuse0 --proxy http://192.168.49.1:8228 --dns virtual -v`

## Connected but no traffic

```bash
ip -brief addr show tetherfuse0                    # expect 198.18.0.1/15
ip route show                                      # default via tetherfuse0?
resolvectl dns tetherfuse0                         # expect 198.18.0.2
resolvectl query example.com                       # virtual DNS working?
curl --noproxy '*' -sI http://example.com | head -1
curl -x http://192.168.49.1:8228 -sI http://example.com | head -1  # proxy itself
sudo tcpdump -ni wlp2s0 tcp port 8228              # only proxy traffic out
sudo tcpdump -ni tetherfuse0                       # app traffic in tunnel
```

## Auto-activation on the hotspot

```bash
nmcli -g connection.secondaries connection show "<hotspot-profile-name>"
cat /etc/tetherfuse/hotspot-uuid                   # dispatcher binding
journalctl -u NetworkManager --since -1h | grep tetherfuse   # teardown events
```
`secondaries` fires when the hotspot **activates**; if you joined the hotspot
before binding, reconnect Wi-Fi once or `nmcli connection up id TetherFuse`.

## Fail-closed problems (locked out / too strict)

```bash
sudo nft list table inet tetherfuse_vpn            # our rules + counters
sudo nft delete table inet tetherfuse_vpn          # immediate relief
nmcli connection modify TetherFuse +vpn.data fail-closed=false
nmcli connection down id TetherFuse && nmcli connection up id TetherFuse
```
Allow local network around the tunnel: `+vpn.data bypass-cidrs=192.168.0.0/16`.
mDNS (printer discovery): `+vpn.data fail-closed-allow-mdns=true`.

## Stale state after backend crash

```bash
pgrep -af tun2proxy                                # orphans? (should be none)
nmcli connection show id TetherFuse                # VPN state per NM
journalctl -u NetworkManager | grep tetherfuse-plugin | tail  # crash reported?
```
The plugin exits the backend process group on disconnect; NM re-runs the
plugin for the next activation. A reboot clears anything truly stuck.

## GNOME entry missing

The profile needs one successful (or attempted) activation to appear in
Quick Settings; Settings → Network lists it immediately after install. There
is no GUI editor for this plugin type (no `[libnm]` editor .so) — configure
via `nmcli connection modify` or the keyfile. This is a documented,
deliberate limitation; the connection itself is a first-class NM VPN.

## Full teardown sanity after disconnect

```bash
ip link show tetherfuse0        # should NOT exist after disconnect
ip route show default           # back to wlp2s0 via DHCP
resolvectl status wlp2s0        # link DNS back to 192.168.49.1
sudo nft list tables            # tetherfuse_vpn absent (fail-closed mode)
```
The plugin never snapshots/restores routes or DNS: NM owns both, so a clean
disconnect restores the live DHCP state, even if you switched networks
mid-tunnel.
