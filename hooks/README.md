# Hooks

Replaceable executables with a JSON contract (`src/bsp/core/hooks.py`).

```text
<hooks-dir>/<kind>/<name>          any executable (python, sh, ...)
stdin   one JSON object (request)
stdout  progress lines, then ONE JSON object on the last line (result)
exit    0 ok | 2 not applicable / nothing found | other = failure ({"error","hint"} in result if possible)
```

Progress lines reach the job log as the hook prints them (flush stdout, or run Python with `-u`). A line
that is a JSON object with a percent, `{"progress": 42}` or `{"type": "progress", "value": 42}`
(optionally with `"msg"`), sets the job's progress (`bsp job get`, `job list`) and shows as `42%` in
its log. On timeout a hook and what it started get SIGTERM, then SIGKILL 10 s later: let go of any
device on SIGTERM.

Search order: `$BSP_HOOKS_PATH` → host config `hook_dirs` → `<site>/hooks` → `packs/*/hooks`
(later packs first) → `~/.config/bsp/hooks` → this directory. Generic hooks live here; hooks for a
public vendor tool or format go in a pack; private procedures go in the site.
Override a built-in by dropping a file with the same `<kind>/<name>` earlier in the path.
Run one by hand: `echo '{"...": ...}' | hooks/probe/usb-devices`.

## The host environment (both kinds below)

The host config's top-level `env:` map reaches every hook three ways: as `req["env"]`, in the
hook's process environment, and expanded in `req["args"]` string values. Support any names:

```yaml
env:
  BSP_TFTP_SERVER: 192.0.2.20
  BSP_SDK_OUTPUT: /srv/sdk-output
  BSP_SERIAL_LOG_DIR: /root/.serial-mux/logs
  BSP_SMTTY: smtty
```

`${NAME}` becomes empty when unset; `${NAME:-default}` uses the default, as in a shell. The
expansion applies to `args` only (not to `package_dir`, `product_dir`, or other paths bsp
computes). The same values are in the hook's environment, so a shell hook's own
`${NAME:-default}` works too.

## Request fields for hooks

| field | meaning |
|---|---|
| `env` | the host config's `env:` map (resolved); in every hook request |
| `network` | `{console, board_subnet, host_address, export_address}` — the DUT's console alias when there is one, the boards' DHCP subnet, and this host's addresses (see `modules.dut.network`); in every hook request |
| `product_dir` | flash and boot-recipe hooks: where this host finds the build's product, from `modules.dut.product_dir` (literal or `{sdk}`/`{artifacts_dir}` template), else the artifact directory; for `bsp dut flash`, the unpacked package |
| `sdk`, `artifacts_dir` | boot-recipe hooks: the SDK instance id and its artifact directory on the build server |

Missing values are omitted rather than sent as null. Hooks must not assume `network` has a
`board_subnet`; it is only there when the site configures it.

## probe/ — board discovery (`bsp probe`)

| hook | request | result |
|---|---|---|
| `probe/consoles` | `{"serial_mux_run": "~/.serial-mux/run", "patterns": ["ttyUSB*","ttyACM*"]}` | `{"consoles": [{"id", "kind": "serial-mux"\|"tty", "alias"?, "device", "usb_port"?, "log"?}]}` |
| `probe/usb-devices` | `{"vids": [...]}` (empty = all non-hub devices) | `{"devices": [{"sysname": "2-1.2", "usb": "usb:2-1.2", "vid", "pid", "devnum", "product"}]}` |
| `probe/identify` | `{"usb", "fastboot", "console_log", "soc", "profile", "timeout"}` — `profile` is the merged `socs/<soc>/soc.yaml` (board in download mode) | `{"soc", "stage": "identified", "fields": {name: value}, "name_hint"}` |

`identify` is generic: it runs the profile's `identify.sequence` (stage an image, `continue`, wait for
console / fastboot regexes), then extracts `identify.fields` from the console, drops
`identify.placeholders` and builds the name from `identify.name`. Images are looked up in the layers
(site before packs) and checked against their `sha256`.

The orchestrator (`src/bsp/dut/probe.py`) owns the risky part — pulsing GPIOs and
correlating reactions — and calls these hooks for everything hardware-specific.

## flash/ — download (flash-to-media) mechanisms, used by boot recipe `hook` steps and `bsp dut flash`

| hook | request | result |
|---|---|---|
| `flash/<name>` | `{"dut", "usb", "console", "fastboot", "artifacts": {role: path}, "args": {...}, "env", "network", "product_dir", "sdk"?, "artifacts_dir"?}` | `{"flashed": [partitions], ...}` |

Boot recipe `hook` steps call them with the SDK's artifacts, `sdk`, `artifacts_dir` and the
resolved `product_dir`. `bsp dut flash` calls them with a package: `package_dir` is the unpacked
package (a directory the flasher may read but not keep; it is removed afterwards) and is also
passed as `product_dir` (that package is the product), `artifacts.package` the package file (and
`artifacts.flash-zip` when it is a zip), `images` the SoC profile's images (`{key: path}`) and
`args` the host config's `modules.dut.flash.args`, expanded from the host env; `timeout` is how
many seconds the hook has before it gets SIGTERM (a flasher with a time limit of its own can
stop a little earlier, cleanly). The board is already in download mode on `usb` unless the
caller asked for `--no-reset`. A flasher reports progress with `{"progress": N}` lines and fails
by exiting non-zero or with an `"error"` (and `"hint"`) in its result. The flasher a board uses
is its `flash.flasher`, else its SoC profile's `flash.flasher`, else `spacemit-pkg`.
