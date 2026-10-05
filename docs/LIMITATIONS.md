# Limitations (read before relying on this)

## Not an encrypted VPN

"TetherFuse" here is a TUN-to-HTTP-CONNECT tunnel. There is no VPN cipher
layer. Privacy properties:

* Traffic to HTTPS sites is protected by TLS end-to-end, as without the tunnel.
* Plain HTTP and DNS-over-UDP-to-the-hotspot are visible to the phone
  (which also runs the proxy — the phone sees everything either way).
* The Wi-Fi link itself carries the proxy TCP stream unencrypted below TCP.

Do not describe or rely on this as a VPN for confidentiality.

## UDP / QUIC / WebRTC / games / VoIP

An HTTP CONNECT proxy tunnels TCP only. **tun2proxy cannot carry arbitrary
UDP through this proxy.** Policy while connected:

* With `fail-closed=true`: unsupported UDP leaving the hotspot interface is
  **rejected with ICMP port-unreachable** (not silently dropped), so QUIC
  browsers fall back to TCP/HTTPS quickly. Direct UDP never leaks.
* With `fail-closed=false` (default): unsupported UDP simply fails inside the
  tunnel unless it happens to work directly (TetherFuse hotspots block direct
  traffic anyway).
* Consequences: HTTP/3 falls back to HTTP/2 (usually seamless), WebRTC calls
  and games may degrade or fail, some voice/video apps break.
* Adding a UDP gateway (tun2proxy `--udpgw-server`) would need extra
  infrastructure and is **out of scope unless explicitly requested**.

## DNS

Default `dns=virtual`: tun2proxy intercepts DNS on the TUN and answers from
a fake pool (`198.18.128.0/17`), resolving through the proxy.

* Works when the hotspot blocks all direct DNS/UDP (TetherFuse hotspots
  block direct traffic).
* systemd-resolved caches answers per its policy; apps using their own DoH,
  hard-coded IPs, or `/etc/hosts` bypass the tunnel DNS by design.
* Reverse lookups and some EDNS behaviour may differ from a real resolver.
* Alternative `dns=over-tcp` sends real-DNS-over-TCP through the proxy to
  `dns-addr` (default 8.8.8.8). Note resolved does not retry UDP-failed
  queries over TCP, so `virtual` is the mode that usually
  works; `over-tcp` suits environments where the client resolver speaks TCP
  directly.

## IPv6

Disabled by default (`ipv6.method=disabled` on the profile, no `-6` to
tun2proxy). AAAA records may still arrive via virtual DNS; browsers will
attempt v6, fail (no v6 route into the tunnel), and fall back to v4
(happy-eyeballs). **If the hotspot itself provides IPv6, v6 traffic would
bypass the tunnel** unless `fail-closed=true` (its rules block non-link-local
v6 on the hotspot interface) or `ipv6-enabled=true` (tunnels v6 through the
proxy when the proxy supports it — untested with TetherFuse).

## Docker / containers

Host traffic is tunnelled; **container traffic is not covered**. Docker's
bridge networking NATs container traffic out via the host's routing — once
the host default route points at the TUN, container TCP flows follow it, but
this is not guaranteed for all network drivers, and container DNS (embedded
resolver) usually targets external resolvers directly. Fail-closed rules do
not touch the forward hook, so published services keep working. Treat
container coverage as an **optional future extension** (per-interface
policy routing / docker DNS proxying), not a property of this release.

## Tailscale / other VPNs

The firewall never touches other interfaces (first rule accepts everything
not leaving the hotspot Wi-Fi interface), so coexisting VPN interfaces
(e.g. tailscale0) are unaffected by design. Still, routing two
default-route claimants is governed by route metrics — if another VPN runs
while TetherFuse activates, check `ip route show` to see which default
wins.

## Manual-disable semantics

Turning the VPN off (GNOME or nmcli) leaves it off **for the current hotspot
connection**. It activates again on the next hotspot connect (that is what
`connection.secondaries` does). There is no watchdog second-guessing you.

## What fail-closed does NOT cover

`fail-closed` scopes rules to the hotspot's Wi-Fi interface only. It does
not firewall other interfaces, forwarded/container traffic, or traffic from
other network namespaces. It is leak prevention for the host's own
direct-connection attempts while the tunnel is up — not a sandbox.

## Verified vs unverified

Everything under "What is verified where" in the README was actually run.
The NM-integration last mile — NM launching the plugin via the `.name` file,
GNOME rendering the VPN entry, `connection.secondaries` auto-activation on
this exact hotspot, suspend/resume — is implemented against verified
interface specifications but requires a root install on the laptop to
confirm. `docs/TROUBLESHOOTING.md` contains the exact checks for each.
