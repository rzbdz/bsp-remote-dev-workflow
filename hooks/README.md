# Hooks

Replaceable executables with a JSON contract (`src/bsp/core/hooks.py`).

```text
<hooks-dir>/<kind>/<name>          any executable (python, sh, ...)
stdin   one JSON object (request)
stdout  progress lines, then ONE JSON object on the last line (result)
exit    0 ok | 2 not applicable / nothing found | other = failure ({"error","hint"} in result if possible)
```

Search order: `$BSP_HOOKS_PATH` → host config `hook_dirs` → `<site>/hooks` → `packs/*/hooks`
(later packs first) → `~/.config/bsp/hooks` → this directory. Generic hooks live here; hooks for a
public vendor tool or format go in a pack; private procedures go in the site.
Override a built-in by dropping a file with the same `<kind>/<name>` earlier in the path.
Run one by hand: `echo '{"...": ...}' | hooks/probe/usb-devices`.

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

## flash/ — download (flash-to-media) mechanisms, used by boot recipe `hook` steps

| hook | request | result |
|---|---|---|
| `flash/<name>` | `{"dut", "usb", "console", "fastboot", "artifacts": {role: path}, "args": {...}}` | `{"flashed": [partitions], ...}` |
