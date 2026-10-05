# Migrating existing proxy settings

If you previously used the hotspot's proxy through application-level
settings — GNOME's manual proxy, `http_proxy`/`https_proxy`/`all_proxy` in
your shell or systemd user session, APT proxy directives — those settings
become **harmful double-proxying** once traffic enters the TUN tunnel:
apps try to speak proxy-protocol through the tunnel or send absolute-URI
requests into the fake-IP space.

Nothing is changed automatically. Recommended migration:

1. **Discover** (read-only): `./discovery.sh` prints GNOME/env/APT/systemd
   proxy settings (credentials masked).
2. **Back up** what you will touch, e.g.:
   ```bash
   cp ~/.bashrc ~/.bashrc.pre-tetherfuse
   gsettings get org.gnome.system.proxy.http host > ~/gnome-proxy.pre-tetherfuse
   ```
3. **Disable only the conflicting pieces, scoped.**
   The cleanest pattern keeps the env exports for use *outside* the
   hotspot and skips them when the tunnel is up:
   ```bash
   # in ~/.bashrc, replacing unconditional exports:
   if ! ip route show default | grep -q tetherfuse0; then
       export http_proxy=http://192.168.49.1:8228
       export https_proxy=http://192.168.49.1:8228
   fi
   ```
   Or remove them entirely and rely on the tunnel for the whole session.
4. **GNOME proxy**: set to `none` for sessions where the tunnel runs:
   ```bash
   gsettings set org.gnome.system.proxy mode 'none'
   ```
   (GNOME proxy settings are per-user, so this only touches your session.)
5. **APT**: if you ever add proxy directives under `/etc/apt/apt.conf.d/`,
   prefer per-host scoping; a global proxy applies system-wide including
   under the tunnel.
6. **Restore**: copy the backups back, or re-run the original
   `gsettings set org.gnome.system.proxy mode 'manual'` command.

Notes:
* The tunnel makes the hotspot usable *without* any proxy env vars — that
  is its purpose. Test with
  `env -u http_proxy -u https_proxy curl -I http://example.com`.
* Keep proxy settings out of `/etc/environment`: they apply system-wide,
  including under the tunnel.
* Never edit other users' dotfiles; everything here is per-user or
  explicitly listed.
