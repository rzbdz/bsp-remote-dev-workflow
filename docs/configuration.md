# Configuration

bsp separates **mechanism** (this repository) from **knowledge about your installation**
(configuration). This page describes where configuration is read from and every file format.

## Layers

Configuration is merged from three layers, lowest priority first:

| Layer | Contents | Location |
|---|---|---|
| Core | Generic hooks (`hooks/`), example site | this repository |
| Packs | Public hardware knowledge: SoC profiles, header pin maps, hooks for public tool formats | `packs/<name>/` in this repository |
| Site | Your hosts, build servers, SDK trees, boot and flash recipes, probe images, boards, hints | first existing of `$BSP_SITE`, `~/.config/bsp/site`, `/etc/bsp/site` |

Each pack and the site can contribute the same kinds of directories: `boards/`, `socs/`,
`hooks/`, `sdk/{families,servers,workspaces,instances}/`, `duts/` and `hints/`. A file in a later
layer replaces the file with the same name in an earlier one; SoC profiles are deep-merged key
by key, so a site can add probe images to a pack's profile.

A rule of thumb for contributors: if anyone who buys the hardware sees the same thing — boot-ROM
output, a header pinout, a vendor's documented image format — it can go in a pack. Anything about
your machines, accounts, trees or procedures belongs in the site.

## Site layout

```text
site/
├── bsp.yaml                 # optional: which packs to use
├── .bspignore               # optional: files deploy/install scripts do not ship
├── remotes.json             # board hosts the CLI talks to
├── hosts/<host>.yaml        # configuration of each bsp-hostd
├── deploy/<host>.env        # how deploy.sh reaches and installs each host
├── boards/<board>.yaml      # GPIO header pin maps (if no pack has yours)
├── socs/<soc>/              # SoC profile overrides and probe images
├── sdk/families/*.yaml      # build styles          ─┐
├── sdk/servers/*.yaml       # build servers           │ see sdk.md
├── sdk/workspaces/*.yaml    # shared source trees     │
├── sdk/instances/*.yaml     # SDK targets            ─┘
├── duts/*.yaml              # boards declared statically (probed ones live on the host)
├── hooks/<kind>/<name>      # site-specific hooks, e.g. a flash tool wrapper
└── hints/*.md               # notes for agents and people
```

`examples/site/` contains a working example of every file; the test suite runs against it.

### bsp.yaml

```yaml
packs: [spacemit]     # omit to use every pack in the repository; [] for none
```

### .bspignore

`tar --exclude-from` patterns (one per line) for site files that `deploy.sh` and
`install-client.sh` must not copy to hosts and clients, such as notes or test records.

### hints/*.md

Free-form Markdown shown by `bsp hints` and included in every `SKILL.md` that `bsp install`
writes. Use them for conventions agents should follow at your site. Hints are copied to hosts and
clients, so do not put secrets in them.

## Host configuration (`hosts/<host>.yaml`)

```yaml
host:
  id: lab-1                      # must match the file name and deploy/<host>.env
  description: lab bench 1
server:
  bind: 0.0.0.0                  # default 127.0.0.1 (local only)
  port: 8765
  state_dir: /var/lib/bsp        # leases, probed boards, runtime GPIO settings, artifact cache
  auth_token: null               # set it whenever bind is not 127.0.0.1, here or as BSP_TOKEN in
                                 # /etc/bsp/hostd.env on the host (see SECURITY.md)
modules:
  gpio_relay:                    # list it before dut: dut drives reset lines through it
    board: example-40pin         # a boards/<id>.yaml from a pack or the site
    backend: sysfs               # sysfs | gpiod | mock
    hw_timeout_s: 3.0            # GPIO calls slower than this mark the backend degraded
    pinmux: true                 # read pinctrl state to flag pins muxed to other functions
    max_pulse_ms: 60000
    protected: [P29]             # pins that must never be driven (probe skips them too)
    channels:                    # baseline channel modes, see gpio-relay.md
      P11: {mode: button}
    aliases:                     # read-only baseline aliases
      bench/fan: P13
  sdk: {}                        # serve the SDK catalog (list/show/plan) over HTTP
  dut:
    smtty: smtty                 # serial-mux client and daemon commands
    serial_mux: serial-mux
    serial_log_dir: ~/.serial-mux/logs
    fastboot: fastboot
    hook_dirs: []                # extra hook directories, searched first
    ssh_console:                 # optional: `ssh bsp-<dut>@host` opens that board's console
      enabled: false
      prefix: bsp-
    slots:
      auto_console: true         # every new serial adapter gets a slot and a serial-mux console of its own
      exclude: []                # ttys that are not slots, e.g. [/dev/ttyACM0] (a relay controller's own port)
    probe:
      pin_order: [history, relay, number]   # which reset lines to try first: lines slots had before,
                                            # lines set up as relays/buttons, then by pin number
      retry_ambiguous: 1         # press a line that made several consoles speak again, this many times
      watch_s: 3.0               # how long to watch consoles/USB after each reset pulse
      pulse_ms: 1000             # reset pulse width while probing
      usb_vids: []               # USB vendor ids to consider (default: from SoC profiles)
      default_soc: null          # used when no boot-ROM fingerprint matches
      default_sdks: {}           # per SoC; empty = every SDK target of that SoC
```

Relative paths are resolved against the directory of the host file. Environment variables
`BSP_BIND`, `BSP_PORT`, `BSP_STATE_DIR` and `BSP_TOKEN` override the `server` keys.

### Site images

Images a host stages (probe images, images a `media_boot` stages) are often not yours to publish. Keep them
out of packs: put the file in `<site>/socs/<soc>/`, or list it in the site's `socs/<soc>/soc.yaml`
with its hash and where it comes from, and keep the file out of git:

```yaml
images:
  handoff:
    file: handoff.bin
    sha256: <hex>
    source: {git: git@git.example.org:team/tools.git, ref: <commit>, path: boot/handoff.bin}
    # or source: {url: https://…}, or source: {file: /path/on/this/machine}
```

`bsp images sync` fetches each listed image no layer holds into `<site>/.blobs/<sha256>/<file>`,
once, and keeps it only if it hashes to its `sha256`; `deploy.sh` runs it before shipping the site,
so hosts get the images and never fetch anything. Put `.blobs/` in the site's `.gitignore`. A newer
image is a new `sha256` and `ref` (or a new entry) and another sync.

### Host registry

A board host that gets its address from DHCP, or sits behind NAT, can tell a registry where it
is instead of being listed by hand in every `remotes.json`. The registry is a `bsp-hostd` with
the `registry` module, typically on a machine every host and client can reach:

```yaml
# hosts/registry.yaml — a registry needs no other module
host: {id: registry}
server: {bind: 0.0.0.0, port: 8765, state_dir: /var/lib/bsp, auth_token: null}
modules:
  registry:
    stale_s: 300          # hosts not heard from for this long are no longer offered to clients
    trust_proxy: false    # true behind a reverse proxy: take the address from X-Forwarded-For
```

Each board host announces itself with `server.announce`:

```yaml
server:
  announce:
    url: http://registry.example.internal:8765
    interval_s: 60        # how often (default 60)
    ssh_user: root        # clients then get an ssh route for tunnels and `bsp dut pipe`
    addresses: []         # extra addresses to offer; default: this machine's own
    token: null           # the registry's API token, if it has one (or $BSP_REGISTRY_TOKEN)
```

Each host also watches for serial devices (`/dev/ttyUSB*`, `/dev/ttyACM*`) and USB devices
coming and going (`modules.dut.watch_ports`, on by default; `{interval_s: 2}` sets how often it
looks, `false` turns it off). A change is published on the host as `dut.ports` and announced to
the registry at once with the host's current ports, and the registry publishes `registry.ports`
with what was added and removed, so a newly cabled board is noticed within seconds.

The registry records the address each announcement came from and offers it first, so a host
behind NAT is reached at the address others see. `$BSP_REGISTRY_URL` sets `announce.url` from
the environment. `bsp status` on the host shows the last announcement; on the registry, `bsp
registry list` (and `GET /api/v1/registry/list`) shows the hosts.

### Console logins

With `modules.dut.ssh_console.enabled: true`, the host keeps one system user per board, named
`<prefix><dut>`. Logging in as that user over ssh (no password) attaches to the board's console
and nothing else: there is no shell, port forwarding is disabled, and Ctrl-A Ctrl-X ends the session (picocom's keys;
Ctrl-C and every other key go to the board).
The users follow the board registry automatically — they are created when a board is added,
renamed with it, and deleted (ending open sessions) when it is removed. Disabling the option
removes the users and every file bsp installed for them. `bsp dut logins` lists the ready-to-use
`ssh` commands. Only enable it on networks where everyone may use the boards' consoles.

## Boards (`duts/*.yaml`)

```yaml
id: example-board
host: lab-1
board: board-x                   # board model, free text
soc: soc-b                       # a SoC profile id
description: shelf 2, left
console: {alias: example-board}  # serial-mux alias; add `device: /dev/ttyUSB0` to start one,
                                 # `prompt: "# $"` to override the SDK's shell prompt
fastboot: {usb: "usb:1-1.2"}     # the selector `fastboot devices -l` shows
control:
  backend: gpio-relay            # gpio-relay | human (alias: manual) | home-assistant (stub)
  reset: {pin: P33, mode: button, pulse_ms: 1000}
  fdl:
    strategy: strapped           # strapped: every reset enters download mode
                                 # button: hold `pin`, pulse reset, release
    # pin: P35
    # expect: {console: "...", fastboot: "..."}   # override the SoC profile's download_mode
  human:                         # human backend only (all optional)
    timeout_s: 900
    reset: "press the red button"
    fdl: "hold BOOT, press RESET, release BOOT"
sdks: []                         # empty = every SDK target of the board's SoC
```

Probed and bound boards are stored on their host; `bsp dut export --dir DIR` writes them in this format, one file per board.

## Clients

### remotes.json

```json
{
  "remotes": {
    "lab-1": {
      "url": "http://192.0.2.20:8765",
      "fallback_urls": ["http://lab-1.example.internal:8765"],
      "ssh": "root@192.0.2.20",
      "token": null,
      "description": "lab bench 1"
    }
  }
}
```

The client tries `url`, then each fallback, then HTTP tunnelled through `ssh` (the host's API
on its loopback interface, reached with `ssh … curl`).

With `"registry": "http://registry.example.internal:8765"` at the top level (or
`$BSP_REGISTRY`), the client also lists every host that announced itself there recently (see
[Host registry](#host-registry)). A host listed in the file as well keeps its token, ssh route
and description, and is tried at its announced addresses first. A registry that cannot be read
is reported by `bsp hosts` and otherwise ignored. `<site>/remotes.json` is shared by all
users of the site; `bsp remote add/remove/use` edits a personal overlay at
`~/.config/bsp/remotes.json` (or `$BSP_CONFIG`, or `$XDG_CONFIG_HOME/bsp/remotes.json`).

### Which hosts a command uses

1. `--url URL`, else `-r NAME[,NAME…]` (case-insensitive, unique prefixes allowed; `-r all`
   for every host), else `$BSP_URL`, else `$BSP_REMOTE`.
2. Without any of these: a `bsp-hostd` with the `dut` module answering on `127.0.0.1:8765`
   (local mode, when you run bsp on a board host), else the default set with `bsp remote use`, else every remote.

Board commands (`bsp dut …`) find the host that owns the board by asking all selected hosts.
Host commands (`bsp gpio …`, `relay`, `button`, …) run on each selected host in turn; commands
that change state refuse to run when more than one host is selected. With `--stateless`
(`BSP_STATELESS=1`) the personal overlay, the saved default and saved lease tokens are ignored
and a host must be named explicitly.

### Files the client writes

| File | Contents |
|---|---|
| `~/.config/bsp/remotes.json` | Personal remotes and the default remote |
| `~/.config/bsp/leases.json` | Lease tokens of the boards and SDKs you hold (mode 0600) |

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `BSP_SITE` | all | Site directory |
| `BSP_URL`, `BSP_REMOTE` | client | Host selection (see above) |
| `BSP_REGISTRY` | client | Registry to list announced hosts from (like `"registry"` in `remotes.json`) |
| `BSP_REGISTRY_URL`, `BSP_REGISTRY_TOKEN` | host | Registry this host announces itself to, and its token |
| `BSP_TOKEN` | client, host | API token: sent by the client (like `--token`); required by the host for changes |
| `BSP_STATELESS` | client | `1`: ignore saved defaults and lease tokens |
| `BSP_LEASE`, `BSP_SDK_LEASE` | client | Lease token for `dut` / `sdk` commands |
| `BSP_HOLDER` | client | Holder name for leases (default `$USER`) |
| `BSP_CONFIG` | client | Path of the personal `remotes.json` |
| `BSP_SDK_CATALOG` | client | An extra SDK catalog directory, highest priority |
| `BSP_SDK_LOCAL` | client | `1`: run SDK operations locally instead of over ssh (on the build server) |
| `BSP_HOOKS_PATH` | host | Extra hook directories (`:`-separated), searched first |
| `BSP_BIND`, `BSP_PORT`, `BSP_STATE_DIR` | host | Override `server.*` in the host config (also settable in `/etc/bsp/hostd.env`) |
