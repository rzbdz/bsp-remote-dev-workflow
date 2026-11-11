# CLI reference

`bsp` is one command for people and programs. Every command accepts `--help`
(`bsp dut boot --help`).

## Global options

They may appear anywhere on the command line.

| Option | Meaning |
|---|---|
| `-r, --remote NAME[,NAME…]` | Board host(s) to talk to (names from `remotes.json`; unique prefixes work; `all` for every host) |
| `--url URL` | Talk to the host at this URL |
| `--token TOKEN` | API token for hosts that require one |
| `--json` | Machine-readable output (see [Output](#output-and-errors)) |
| `--stateless` | Ignore the saved default host and saved lease tokens; for parallel agents and CI |

How hosts are chosen without `-r`/`--url` is described in
[configuration.md](configuration.md#which-hosts-a-command-uses).

## Boards: `bsp dut`

| Command | Purpose |
|---|---|
| `bsp dut list [--sdk SDK] [--soc SOC] [--free]` | Boards on all reachable hosts, with their slots |
| `bsp dut status DUT` | Stage, lease, fastboot state, control lines, pending manual action, console tail |
| `bsp dut log DUT [--lines N] [--grep RE]` | Recent console lines, read passively |
| `bsp dut logs DUT [--since T] [--until T] [--grep TEXT [--regex]] [--limit N] [--first]` | The console log over a time range (`YYYY-MM-DD HH:MM:SS`), searched; says when it was capped |
| `bsp dut acquire DUT [--holder NAME] [--purpose TEXT] [--ttl S]` | Lease a board (default 3600 s, at most 24 h); the token is remembered |
| `bsp dut renew DUT [--ttl S]` | Extend your lease from now; holder, purpose and token stay the same |
| `bsp dut release DUT [--force]` | Release it |
| `bsp dut reset DUT [--no-fdl] [--no-wait] [--timeout S]` | Reset into download mode and wait for it (`--no-fdl`: boot the board's own system; a board whose FDL is strapped boots its media through the SoC's media boot when it has one) |
| `bsp dut boot DUT --sdk SDK [--method load\|download] [--variant V] [--no-wait] [--timeout S]` | Run the SDK's boot recipe; streams progress, exit code is the result |
| `bsp dut boot DUT --files FILE… [--no-reset] [--first-wait S] [--later-wait S] [--port-timeout S] [--no-wait]` | Boot from RAM with local files in order (see [Booting local files](#booting-local-files)) |
| `bsp dut flash DUT --package PATH\|URL [--sha256 HEX] [--flasher NAME] [--timeout S] [--no-reset] [--no-wait]` | Write a package (images and their layout) to the board's media (see [Flashing a package](#flashing-a-package)) |
| `bsp dut stage DUT FILE [--no-continue] [--timeout S]` | Send a local file to the host (once, by sha256) and load it into RAM: `fastboot stage` + `continue` |
| `bsp dut present DUT` | Whether the board's download/fastboot port is on the USB bus |
| `bsp dut console DUT "CMD" [--expect RE] [--timeout S]` | Run one shell command on the console and print its output |
| `bsp dut console DUT` | Attach interactively over ssh (Ctrl-A Ctrl-X detaches, as in picocom) |
| `bsp dut pipe DUT` | Raw serial-mux protocol on stdin/stdout, for programs (see [Console pipe](#console-pipe)) |
| `bsp dut confirm DUT [NOTE]` | Tell a waiting reset/boot that the requested manual action is done |
| `bsp dut logins` | `ssh` commands that open each board's console (if enabled on the host) |
| `bsp dut bind NAME --console TTY\|ALIAS --usb USB [--soc SOC] [--board B] [--description D] [--sdks LIST] [--fdl strapped\|button] [--host HOST] [--replace]` | Register a board from a console and a USB port; a person does the resets |
| `bsp dut add HOST --spec JSON\|@FILE [--replace]` | Register a board from a spec (what probe produces) |
| `bsp dut set DUT [--board/--soc/--description/--sdks/--usb/--reset-pin/--pulse-ms/--fdl/--fdl-pin]` | Edit a board |
| `bsp dut rename OLD NEW [--console]` | Rename a board (and its console alias with `--console`) |
| `bsp dut refresh DUT [--name NEW \| --rename] [--board B] [--soc S]` | Re-read the board in a slot after swapping it: reset into download mode, identify, update its board, SoC and model, and its download port if the cable moved; the lease stays |
| `bsp dut remove DUT [--force]` | Forget a board (its slot stays) |
| `bsp dut export [--dir DIR] [--host HOST]` | Registered boards as YAML; `--dir` writes one `DIR/<id>.yaml` per board, the format of `SITE/duts/` |

**Slots.** Every `DUT` above can also be named by its slot: `<host>/s3`, or `s3` with one host.
A slot is a serial console on a host (see [Slots](#slots-bsp-slot)), so a slot name keeps working
when the board in it is swapped and refreshed.

**Leases.** `reset`, `boot`, `stage`, `console "CMD"`, `renew`, `release` and `refresh` need your lease; `set` and `rename`
need it while the board is leased. The CLI passes the remembered token; in stateless mode use
`--lease TOKEN` or `BSP_LEASE`.

**Editing boards.** `set`, `rename` and `remove` work on boards registered on the host (by
`probe`, `bind` or `add`). Boards defined in `SITE/duts/` are edited in their file.

### Slots: `bsp slot`

Every serial console on a board host is a slot: each USB-UART adapter, in the physical USB port it
is plugged into, gets a slot and a serial-mux console of its own as soon as the host sees it. A
reset line, a download port and a board are attached to a slot as they are learned (by `probe`,
`bind` or `add`); a slot without them is still a console you can watch and type into, which is
what you want while wiring a new board or debugging. Removing a board leaves its slot, and the slot
remembers the reset lines, download ports and boards it had. When an adapter is unplugged, its slot
goes away if it held no board; a slot holding a board stays, offline, so the board shows as
unreachable until it comes back, is removed, or the host is rebuilt.

After re-cabling a host (consoles in other USB ports, boards moved), rebuild it:
`bsp probe --rebuild -r HOST --yes`. Every board the probe registered and every slot is forgotten.
Each adapter plugged in now gets a fresh slot, and every board is found again, trying the reset
lines boards used before first and giving each its old name back. Boards from the site's `duts/`
stay. It refuses to start while a board on the host is leased or busy.

| Command | Purpose |
|---|---|
| `bsp slot list [--free]` | Slots on all reachable hosts: console, online or not, board, reset line, download port |
| `bsp slot status SLOT` | One slot, with its history |
| `bsp slot console SLOT` / `bsp slot pipe SLOT` | Its console, interactively or for programs, with or without a board |
| `bsp slot scan` | Match each host's consoles to slots now (hosts also do this when cables change) |
| `bsp slot forget SLOT` | Forget a slot whose adapter is gone |

### Boards reset by a person

Boards with the `human` control backend (for example slots made with `bsp dut bind`) have no
reset wiring. `bsp dut reset` and `bsp dut boot` then print what to do and wait:

```console
$ bsp dut reset slot-1
slot-1 @ lab-1: reset needs a person, job reset-1793860731-1 (confirm with `bsp dut confirm slot-1`)
  [   0.0s] ACTION NEEDED: press RESET on slot-1 (board-x); it boots into download mode
  [   0.0s] waiting for `bsp dut confirm slot-1` or for the board to show it on its own (timeout 900s)
```

The wait ends when someone runs `bsp dut confirm slot-1`, or as soon as bsp sees the board in
download mode on its console or USB port (for a plain reset: the SoC's boot-ROM output on the
console). If the board is not actually in download mode after a confirmation, the next step
fails with a fastboot timeout. Scripts and agents should run such commands in the background.

### Booting local files

`bsp dut boot DUT --files FSBL.bin u-boot-opensbi.itb Image.itb` boots a board from RAM with
files from your machine. Each file is sent to the board host once (by sha256; unchanged files
are not sent again, over HTTP or the ssh tunnel), then the host puts the board into download
mode (skip with `--no-reset`) and for each file runs `fastboot stage` + `continue` and waits for
the download port to come back before the next one: `--first-wait` seconds after the first file
(default 8, while the boot ROM hands over), `--later-wait` after later ones (default 3), then up
to `--port-timeout` more (default 20). If the port does not return, the boot stops and names the
file. It runs as a background job like `--sdk` boots, with the path and sha256 of every file in
the job log. Nothing is flashed.

Programs can do the same over HTTP: `PUT /api/v1/artifacts/<sha256>?name=FILE` with the file as
the body (checked against the sha256; `GET` the same URL to see whether it is cached), then
`dut_stage` or `dut_boot_files` with the sha256 values.

### Flashing a package

`bsp dut flash DUT --package PKG` writes a package to the board's storage. `PKG` is a local
`.tar.gz`, `.tgz`, `.tar` or `.zip`, or a local directory (sent as a `.tar`), or an `http(s)://`
URL that the board host downloads itself (into its artifact cache, checked against `--sha256`
when given; other schemes are refused). A host takes packages up to 16 GiB by default
(`modules.dut.flash.max_package_bytes` in the host config). The host unpacks the package into a
fresh directory (entries reaching outside it, links and special files are refused; an archive
holding a single top-level directory unpacks to that directory), puts the board into download
mode (skip with `--no-reset`) and runs the flash hook `flash/<flasher>` on the directory (see
[hooks/README.md](../hooks/README.md)).

The flasher is `--flasher`, else the board's `flash.flasher`, else its SoC profile's
`flash.flasher`, else `spacemit-pkg` (a package with `fastboot.yaml` and `partition_*.json`
layouts, or a flash zip). The flash runs as a background job of kind `flash` under your lease;
when the flasher reports progress the job has a percent (`bsp job get`, `bsp job list`) and the
CLI prints it as it comes. With `--json` the result is `{"job", "lines", "elapsed_s", "flasher",
"flashed"}`. `--timeout` (default 3600 s) bounds the flasher; when it runs out the flasher gets
SIGTERM first so it can let go of the USB device.

Over HTTP: upload the package as an artifact (see above), then `dut_flash` with `package` set to
its sha256, or to the URL.

### Console pipe

`bsp dut pipe DUT` connects stdin and stdout to the board's serial-mux daemon and copies bytes
both ways until either side closes. The bytes are serial-mux's own protocol (4-byte length,
then JSON), so any serial-mux client can open a session through it; it is meant for programs
such as dashboards, not for typing. On a remote host the CLI runs a small Python bridge there
over the remote's `ssh` entry; on the host itself it connects to the socket directly. No lease
is needed: reading is open to everyone (as with `dut log`), and input is subject to the
console's serial-mux input gate. `bsp dut status` reports the socket as `console_socket` and
whether the daemon is up as `console_up`.

## Discovery and hosts

| Command | Purpose |
|---|---|
| `bsp hosts` | Reachable hosts, how they are reached, their modules and boards |
| `bsp -r REGISTRY registry list [--all]` | Hosts that announced themselves to a registry (see [configuration.md](configuration.md#host-registry)) |
| `bsp init` | First-run check: configuration directory, mode, hosts |
| `bsp probe --rebuild -r HOST [--yes] [--no-identify] [--sdks LIST]` | After re-cabling: forget the host's probed boards and slots, then rediscover every board (without `--yes`: say what would be forgotten) |
| `bsp probe [--slots S..] [--pins P..] [--known] [--register] [--sdks LIST] [--dry-run] [--yes] [--no-identify] [--replace]` | Discover the boards on free slots (pulses GPIO lines — see [getting-started.md](getting-started.md#option-a-probe-boards-wired-to-gpio-reset-lines)). Lines slots had before are tried first, so re-cabled or re-registered boards come back fast (`--known`: only those); a line that made several consoles speak is tried again at the end. `--register` (implied by `--yes`) registers each board on its host the moment it is found, named after what identify read, else after the board its slot had last |
| `bsp -r HOST probe --human [--rounds N] [--wait S] [--consoles C..] [--yes]` | Discover boards without reset wiring: a person puts one unregistered board at a time into download mode, and bsp pairs the console that speaks with the USB port that appears; each becomes a slot reset by a person, as with `dut bind` |
| `bsp remote list\|show\|add\|remove\|use` | Manage personal host entries |
| `bsp watch [--registry]` | Stream live events from the host(s), or with `--registry` from the registry (hosts announcing themselves, `registry.ports` when a host's cables change); `--json` prints one event per line for programs |
| `bsp tools` | Print the host's AI tool schemas in Anthropic format (OpenAI format: `GET /api/v1/tools?format=openai`) |
| `bsp hints` | Hints from the configuration layers (packs, site) |
| `bsp images [list\|sync]` | Images hosts stage (probe images, media-boot helpers): where each comes from and whether the site has it; `sync` fetches the missing ones once into `<site>/.blobs/` (deploy does this) |
| `bsp install [--dir DIR] [--offline]` | Write `SKILL.md` for agents working in an SDK tree (see [agents.md](agents.md)) |

## SDKs: `bsp sdk`

| Command | Purpose |
|---|---|
| `bsp sdk list [--soc SOC]` | SDK targets, their SoC, workspace and boot-method status |
| `bsp sdk show SDK` | Build server, path, dev repos, components, artifacts, sharing |
| `bsp sdk status SDK` | Lease, branches, uncommitted files, recent builds |
| `bsp sdk plan SDK [COMPONENT] [--boot load\|download] [--variant V]` | Dry run: the build command or the boot steps |
| `bsp sdk acquire SDK --task NAME [--holder NAME] [--ttl S] [--branch B] [--projects LIST]` | Lease the workspace (default 8 h) and switch to branch `bsp/NAME` |
| `bsp sdk release SDK [--keep] [--force]` | Restore the owner's branch and stashed work |
| `bsp sdk build SDK COMPONENT [--clean] [--no-wait] [-v] [--timeout S]` | Build on the server; streams warnings and errors |
| `bsp sdk jobs SDK`, `bsp sdk job SDK ID [--follow] [--log]` | Build jobs |
| `bsp sdk exec SDK [--cwd DIR] [--read-only] [--timeout S] -- CMD…` | Run a command in the tree (cwd: first dev repo) |
| `bsp sdk get SDK PATH [-o FILE]`, `bsp sdk put SDK PATH FILE` | Read or write a file in the tree |

Commands that change the tree (`build`, `exec`, `put`) are refused while someone else holds the
workspace lease; hold it yourself while you work.

`exec` joins its arguments and runs them with `bash -c` on the build server, so quote once more
for words that must stay together: `bsp sdk exec SDK -- git commit -am "'usb: fix resume'"`.

See [sdk.md](sdk.md) for what acquire and release do to the tree.

## Host commands

These run on a board host; the CLI fetches their definitions from the host, so they are always
in sync with it. Add `-r HOST` when several hosts are reachable.

| Group | Commands |
|---|---|
| `bsp gpio` | `list`, `get`, `set`, `pulse`, `free`, `config`, `export`, `pinmux`, `alias add\|delete\|list\|resolve` |
| `bsp relay` | `on`, `off`, `toggle`, `pulse` |
| `bsp button` | `press`, `release`, `click` |
| `bsp board` | `info` |
| `bsp job` | `list`, `get` |
| `bsp events` | `recent` |
| `bsp status` | Service, module and backend health |

The same commands are HTTP endpoints (`GET` for reads, `POST` for changes, under `/api/v1/`) and
AI tools; `GET /api/v1/tools` lists them and `/docs` serves the OpenAPI description.

## Output and errors

Text output is meant for people. With `--json` every command prints exactly one object:

```json
{"ok": true, "result": {"...": "..."}}
{"ok": false, "error": {"code": "not_found", "message": "no DUT 'example-bord' on any reachable host",
                        "hint": "did you mean: example-board? known: example-board@lab-1", "details": {}}}
```

`hint` names the next step (a likely typo, who holds a lease, which command to run); `details`
carries context such as the console tail or the end of a build log. Long-running commands print
progress lines to stderr in JSON mode.

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | The command failed |
| 2 | Usage error, or an ambiguous host for a state-changing command |
| 3 | No host reachable |
| 130 | Interrupted |
