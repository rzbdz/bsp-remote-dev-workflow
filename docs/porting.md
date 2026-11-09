# Porting

bsp keeps hardware knowledge in data files and small hooks. Adding support for new hardware
rarely needs code.

## A new SoC

Create `socs/<soc>/soc.yaml` — in a pack if the information is public, otherwise in your site:

```yaml
soc: soc-a
fingerprint:                     # boot-ROM console lines printed after a reset
  - "ROMA: boot start"
  - "ROMA: usb init"
download_mode:                   # how bsp recognises USB download (FDL) mode
  console: "download handler"    # console regex (optional)
  fastboot: "DFU download"       # fastboot product regex
usb_vids: ["1d6b"]               # vendor ids of the download/fastboot gadgets (probe filter)
images:                          # known-good bootloader images for probing (RAM only)
  fsbl: {file: fsbl.bin, sha256: "…"}
  uboot: {file: uboot.itb, sha256: "…"}
identify:                        # how probe reads a board's identity
  sequence:                      # stage an image, continue, wait for console/fastboot
    - {stage: fsbl, console: "SPL", fastboot: "Fastboot"}
    - {stage: uboot, console: "U-Boot 20", fastboot: "Fastboot"}
  fields:                        # regexes over the console output that follows
    product: "board_name = (.*?)\\s*$"
    serial: "serial# = (.*?)\\s*$"
    mem_gib: "DRAM:\\s*(\\d+) GiB"
  placeholders: {serial: ["0", "none"]}   # values meaning "not programmed"
  name: [product, mem, serial]            # parts of the proposed board name
media_boot:                      # optional: how a board whose FDL strap is held down boots its media
  name: rom-handoff              # shown in `bsp dut status`
  steps:                         # run after a reset into download mode; recipe steps (sdk.md)
    - {do: check, serial: "strap: nor"}            # the boot ROM banner: a board these steps suit
    - {do: getvar, name: rom-version, match: "^2"}
    - {do: stage, image: handoff}                  # a key in images, verified by sha256
    - {do: continue}
    - {do: wait, serial: "SPL 20", timeout: 60}
```

- `fingerprint` lets `bsp probe` tell SoCs apart; pick lines that appear on every cold boot and
  are specific to the boot ROM.
- `download_mode` is used by every `enter-fdl` step and by the human reset backend's
  auto-detection.
- Probe images are looked up in every layer, highest first, and checked against `sha256`. Ship
  only images you may redistribute in a pack; otherwise keep them in the site, or list them there
  with a `source` and let `bsp images sync` fetch them (see [configuration.md](configuration.md#site-images)).
- `media_boot` is for SoCs that can boot a board's media from download mode. A plain reset
  (`bsp dut reset --no-fdl`) and recipe steps marked `when: fdl-not-strapped` reset the board into
  download mode and run these steps. Use `check` and `getvar … match` first, so a board the steps
  do not suit is refused before anything is staged. Steps may also run a hook (`do: hook`). Boards
  with an FDL line never need this. A DUT can bring its own `control.media_boot`, or switch it off
  with `control.media_boot: false`. `bsp dut status` shows how each board boots its media.

Then write boot recipes for the SDKs of that SoC ([sdk.md](sdk.md)).

## A new board host type

Describe its GPIO header in `boards/<id>.yaml`: one entry per header with its pins — a GPIO pin
gives its Linux GPIO number (`gpio: 83`), other pins a `kind` (`power`, `gnd`) and `label` —
plus optional alternate-function labels and where the kernel's pin-mux information lives. See
`examples/site/boards/example-40pin.yaml` and the files in `packs/spacemit/boards/`. Select it
with `modules.gpio_relay.board` in the host configuration.

GPIO access goes through a backend: `sysfs`, `gpiod` (installed automatically by `deploy.sh`) or `mock`. A new
backend implements the `GpioBackend` interface in `src/bsp/gpio_relay/backends/` and registers
itself.

## A new reset mechanism

Control backends live in `src/bsp/dut/control.py`: subclass `ControlBackend`, implement
`reset(log, ready)` (and `enter_fdl` if the board needs more than a reset), and register it with
`@CONTROL_BACKENDS.register("name")`. Boards select it with `control.backend`.

## Hooks

Procedures that do not fit recipe steps — a vendor flash tool, a different way to list consoles
— are hooks: executables with a JSON-in, JSON-out contract, searched in the site, then the packs,
then the core. See [hooks/README.md](../hooks/README.md).

## Packs

A pack is a directory under `packs/` with a `pack.yaml` and any of `socs/`, `boards/`,
`hooks/` and `hints/`:

```yaml
name: acme
description: ACME A1 SoC profile and evaluation-board header map
provides:
  socs: [a1]
  boards: [acme-a1-evb]
```

Packs contain only public knowledge: anything a person who buys the hardware can observe or
download. Sites choose packs with `packs:` in `bsp.yaml`. Contributions of new packs are welcome;
see [CONTRIBUTING.md](../CONTRIBUTING.md).
