# TetherFuse NM VPN Integration

Turns a TetherFuse phone hotspot's HTTP CONNECT proxy into a genuine
**NetworkManager VPN connection** — native GNOME VPN toggle, system-wide
routing, DNS included — with no external VPN server and no phone
modification.

```
applications (no proxy settings needed)
  → TUN device (created by tun2proxy, managed by NetworkManager)
    → tun2proxy (pinned v0.8.4)
      → TetherFuse HTTP CONNECT proxy on the phone
        → internet
```

> **This is proxy tunnelling, not an encrypted VPN.** There is no extra
> cipher layer; HTTPS protects what HTTPS always protected. See
> [docs/LIMITATIONS.md](docs/LIMITATIONS.md) before relying on it.

## Requirements

* Ubuntu-style Linux with NetworkManager ≥ 1.4x and systemd-resolved
* Python 3 with `python3-gi` (preinstalled on GNOME systems)
* `nftables` (only for the optional fail-closed mode)
* The tun2proxy backend — the installer can download it pinned and
  sha256-verified, or use `--tun2proxy-bin` / `--tun2proxy-zip`

## Quick start

```bash
./discovery.sh        # optional: read-only look at your environment
sudo ./setup.sh       # interactive: pick the hotspot, enter proxy, install
nmcli connection up id TetherFuse
```

`setup.sh` prompts as your normal user (Wi-Fi profile menu with your
currently connected network preselected, proxy host:port — the TetherFuse
default `192.168.49.1:8228` works unless you changed it — optional
fail-closed mode and backend download), then runs `install.sh` via sudo.
After the first successful activation the connection appears in GNOME's
Quick Settings and Settings → Network as a VPN.

Manual, non-interactive equivalent:

```bash
sudo ./install.sh --proxy 192.168.49.1:8228 \
                  --bind-hotspot <hotspot-profile-UUID> \
                  [--install-backend] [--fail-closed]
# hotspot UUIDs: nmcli -t -f NAME,UUID,TYPE connection show | grep 802-11
```

## How it works

| Piece | Role |
|---|---|
| `src/tfvpn/plugin.py` | NetworkManager **VPN service plugin** implementing `org.freedesktop.NetworkManager.VPN.Plugin` on the system bus (interface verified against NM 1.52/1.54 source) |
| `src/tfvpn/logic.py` | Connection state machine: spawn backend → wait for the TUN → TCP-probe the proxy (bounded retries for slow hotspots) → report `Config`/`Ip4Config` → STARTED; crashes and timeouts produce accurate NM failure codes |
| `src/tfvpn/backend.py` | tun2proxy supervision. Runs **without** `--setup`: NetworkManager owns routing and DNS, so nothing fights over routes or resolv.conf |
| `src/tfvpn/firewall.py` | Optional fail-closed nftables table scoped to the hotspot interface only — other interfaces, Docker, UFW and other VPNs are untouched; unsupported UDP is *rejected* (fast QUIC→TCP fallback), never leaked |
| `data/70-tetherfuse` | NM dispatcher: tears the VPN down when the hotspot profile disconnects |
| activation | `connection.secondaries` on the hotspot profile (NM's sanctioned mechanism; VPN `autoconnect` does not exist). Turning the VPN off manually stays off until the hotspot reconnects |

**DNS strategy:** tun2proxy's *virtual DNS*. The plugin reports `198.18.0.2`
(on-link, distinct from the tunnel address) as the tunnel's DNS server;
resolved queries it through the TUN, tun2proxy answers from a fake-IP pool
(`198.18.128.0/17`), and traffic to those fake IPs is carried through the
proxy with real hostnames. This works even on hotspots that block all
direct DNS/UDP. The pool deliberately excludes the interface and DNS
addresses — see the validation in `config.py` (both collision types were
field-found bugs).

## Configuration

Everything lives in the NM profile (`vpn.data`), editable with
`nmcli connection modify TetherFuse +vpn.data key=value`. Full reference
in [data/TetherFuse.nmconnection.example](data/TetherFuse.nmconnection.example).
Highlights:

| Key | Default | Meaning |
|---|---|---|
| `proxy-host` / `proxy-port` | required | phone proxy endpoint |
| `route-all` | `true` | `false` = split tunnel via `extra-routes` |
| `bypass-cidrs` | — | CIDRs allowed around the tunnel (passed to tun2proxy) |
| `fail-closed` | `false` | scoped nftables leak prevention while connected |
| `dns` | `virtual` | `virtual` / `over-tcp` / `direct` |
| `max-sessions` | `4096` | tun2proxy concurrent TCP cap (upstream default 200 is too small for a system-wide default route) |
| `tun-address` / `virtual-dns-ip` / `virtual-dns-pool` | `198.18.0.1` / `198.18.0.2` / `198.18.128.0/17` | must be mutually disjoint; validated |
| `ipv6-enabled` | `false` | tunnel IPv6 through the proxy (see LIMITATIONS first) |

## Daily use

```bash
nmcli connection up id TetherFuse        # or the GNOME VPN toggle
nmcli connection show id TetherFuse      # state, tunnel address
nmcli connection down id TetherFuse
journalctl -u NetworkManager -f | grep tetherfuse   # plugin FSM log
resolvectl dns tetherfuse0               # expect 198.18.0.2
```

The plugin's logs land in the NetworkManager unit's journal (NM adopts the
plugin process), so grep the NM unit — `journalctl -t tetherfuse-plugin`
shows nothing.

**Updating after `git pull`/edits:** NM keeps the plugin process alive
between activations, so after re-running `sudo ./install.sh` restart the
service too:

```bash
nmcli connection down id TetherFuse
sudo pkill -f tfvpn-plugin
nmcli connection up id TetherFuse
```

## Verification status

* **Automated suite** (`python3 -m pytest tests/`, needs `python3-gi`):
  config validation/injection resistance, tun2proxy argv construction
  (checked against the real v0.8.4 binary's `--help`), firewall ruleset
  scoping, the full connection FSM (delayed proxy, crash reporting,
  disconnect, split tunnel, fail-closed, timeouts), a **live session-bus
  D-Bus cycle** against a real plugin process (Connect → Config →
  Ip4Config → STARTED → Disconnect → STOPPED), and a mock CONNECT proxy.
* **Field-tested** on Ubuntu 26.04 / GNOME 50 / NetworkManager 1.54
  against a live TetherFuse hotspot: NM launches the plugin from the
  `.name` file, the tunnel becomes the IPv4 default route with working
  virtual DNS, proxy-less applications (env vars unset) reach the internet,
  failure/crash propagation is accurate, and deactivate/reactivate cycles
  are clean with no duplicate processes or routes.
* **Needs hands-on confirmation per machine:** GNOME entry rendering,
  hotspot auto-activation timing, suspend/resume. Root-only data-path e2e
  without a phone: `sudo tests/netns_e2e.sh` (uses the bundled mock proxy).

## Uninstall

```bash
sudo ./uninstall.sh --remove-profile
```

Removes only what this integration owns (hash-verified manifest); keeps
files you modified since, all Wi-Fi profiles, other VPNs, Docker/UFW
rules. Backups of overwritten files live under `/var/backups/tetherfuse-nm/`.

## Documentation

* [docs/LIMITATIONS.md](docs/LIMITATIONS.md) — UDP/QUIC, encryption reality,
  IPv6, Docker, DNS interactions
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — command cookbook
* [docs/MIGRATION.md](docs/MIGRATION.md) — existing GNOME/env/APT proxy settings

## License

[MIT](LICENSE) — the bundled tun2proxy backend is licensed separately
upstream (tun2proxy/tun2proxy on GitHub); this repository's code covers only
the NetworkManager integration itself.
