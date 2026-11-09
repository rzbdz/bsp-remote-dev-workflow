# Deployment

## Board hosts: `deploy/deploy.sh`

```bash
deploy/deploy.sh HOST [deploy|status|logs|restart|ssh] [--allow-dirty]
```

| Action | Effect |
|---|---|
| `deploy` (default) | Ship code and site, install, (re)start the service, health check |
| `status` | `systemctl status bsp-hostd` and the service's status endpoint |
| `logs` | The service's journal; `LINES=200 deploy/deploy.sh HOST logs` for more than the default 100 lines |
| `restart` | Restart the service |
| `ssh` | Open a shell on the host |

The script reads `SITE/deploy/HOST.env` and requires `SITE/hosts/HOST.yaml`. It ships the
committed tree of this repository (including the serial-mux submodule); `--allow-dirty` also
ships uncommitted changes and new, non-ignored files. The site directory is shipped as a whole,
minus `.git` and the patterns in `SITE/.bspignore`.

### Target file (`SITE/deploy/HOST.env`)

| Key | Meaning |
|---|---|
| `SSH_HOST`, `SSH_USER`, `SSH_PORT` | How to log in to the host (the installer needs root) |
| `SSH_JUMP` | Optional `user@host` to reach the target through a jump host |
| `INSTALL_DIR` | Installation directory, e.g. `/opt/bsp-dev-loop` |
| `SERVICE_PORT` | Port of the service (must match `server.port`) |
| `HEALTH_URL` | URL this machine uses for the final health check (default: `EXTERNAL_URL`; with neither set, only the host's own check of `127.0.0.1` runs) |
| `EXTERNAL_URL` | URL printed at the end, for people |
| `UPNP_PORTS` | Router port mappings to register via UPnP; leave empty (see below) |

If the host configuration selects the `gpiod` GPIO backend, the installer adds the `gpiod`
Python package; the host needs the `libgpiod` system library (e.g. `apt install libgpiod2`).

Authentication uses your ssh keys. For hosts that only accept a password, export `SSHPASS` and
install `sshpass`; the password is never written to disk by bsp.

### What is installed

| Path / unit | Contents |
|---|---|
| `INSTALL_DIR/app` | The shipped bsp tree (`app.old` keeps the previous one) |
| `INSTALL_DIR/site` | The shipped site (`site.old` keeps the previous one) |
| `INSTALL_DIR/venv` | Python environment with bsp and serial-mux |
| `/usr/local/bin/bsp` | The CLI, in local mode on the host |
| `bsp-hostd.service` | The service: `bsp-hostd --config INSTALL_DIR/site/hosts/HOST.yaml` with `BSP_SITE` set |
| `bsp-console-<alias>` | One transient unit per serial console, started on demand |
| `/var/lib/bsp` (`server.state_dir`) | Leases, probed boards, runtime GPIO settings, artifact cache |

With console logins enabled, the service also manages `bsp-*` system users, a `bsp-console`
group, `/etc/ssh/sshd_config.d/50-bsp-console.conf`, `/etc/sudoers.d/bsp-console` and helpers in
`/usr/local/lib/bsp/`; disabling the option removes all of them (see
[configuration.md](configuration.md#console-logins)).

### UPnP

For a host behind a home or lab router, `UPNP_PORTS` registers port mappings with a systemd
timer (`bsp-upnp.timer`); it needs `upnpc` (package `miniupnpc`) on the host. This exposes the
API beyond the local network: only use it with an API token set, and prefer a VPN or an
ssh `remotes.json` entry instead.

## Build-server access: `deploy/grant-build-access.sh`

```bash
deploy/grant-build-access.sh HOST USER@BUILD_SERVER [USER@BUILD_SERVER …]
```

Creates an ssh key for `SSH_USER` on the board host (if missing; this is root, the user the
service runs as) and authorises it on each build server account, so the host can fetch build
artifacts. It reads `SITE/deploy/HOST.env` and honours `SSH_PORT` and `SSH_JUMP` like
`deploy.sh`. This machine needs ssh access to the host and to every build server.
Use a dedicated build-server account if you do not want the host to have your own.

### Running `bsp-hostd` by hand

```bash
bsp-hostd -c SITE/hosts/HOST.yaml [--bind ADDR] [--port PORT] [--log-level debug]
```

`BSP_SITE` must point at the site. This is what the systemd unit runs; it is also how to try a
configuration locally (see the mock host in the README).

## Clients: `deploy/install-client.sh`

```bash
deploy/install-client.sh USER@MACHINE|local [DIR] [--site SITE_DIR] [--allow-dirty]
```

Copies the committed tree to `DIR` (default `~/bsp-dev-loop`), links `~/.local/bin/bsp`, and with
`--site` installs a copy of the site (without `.git`) at `~/.config/bsp/site`. It refuses to
replace a `~/.config/bsp/site` that is a symlink or a git checkout, so a site repository kept
there is never overwritten. It uses ssh keys only (`BatchMode`). The
client needs Python ≥ 3.8 and PyYAML.

Alternatively, `pip install -e .` in a clone (Python ≥ 3.10) installs the `bsp` command with its
dependencies.

## Agent skills: `deploy/install-skills.sh`

```bash
deploy/install-skills.sh [USER@MACHINE|local]
```

Copies `skills/*` into the skill directories of the agents present on the machine: Claude Code
(`~/.claude/skills`), opencode (`~/.config/opencode/skill`) and Hermes
(`~/.hermes/skills/embedded`). See [agents.md](agents.md).
