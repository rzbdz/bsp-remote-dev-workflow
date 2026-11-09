# spacemit pack

Public hardware knowledge about SpacemiT K1 / K3 parts — what anyone who buys these boards
can observe. No SDK layouts, build servers or flash procedures of a particular installation
(those belong in a site).

| path | what |
|---|---|
| `socs/k1/soc.yaml`, `socs/k3/soc.yaml` | boot-ROM fingerprint, download mode (console + fastboot product), USB vendor id, U-Boot identity patterns (TLV) and the probe sequence; image *names* only — known-good images go in `<site>/socs/<soc>/` |
| `boards/spacemit-k3-com260.yaml` | 40-pin header map of the K3 Com260 host-controller board |
| `boards/spacemit-k1x-deb1.yaml` | 26-pin header map of the K1-x deb1 board |
| `hooks/flash/spacemit-pkg` | flash a `pack_image` zip (`partition_*.json` layouts) over fastboot |
| `hints/spacemit.md` | facts agents need on these boards |

## Hardware notes

- Boards with FDL strapped enter boot-ROM download mode on every reset: a reset is also "enter
  FDL", and a cold boot from flashed storage cannot be observed on them.
- The fastboot USB path stays the same across BROM → SPL → U-Boot on a given port.
- U-Boot prints the TLV identity on the console (`TLV item: product_name = ...`);
  `fastboot oem config:read` answers OKAY but the fastboot CLI does not show the value, so probe
  reads the console.
- K3 Com260 (Linux 6.x vendor kernel): **never read**
  `/sys/kernel/debug/pinctrl/*/pinconf-pins` (or `pins`): `spacemit_pinconf_dbg_show` oopses and
  leaves the pinctrl lock held, after which GPIO export hangs in D state until reboot. bsp only
  reads `pinmux-pins` / `gpio-ranges`, with a timeout and single-flight guard.
- K3 Com260 header: P3/P5 (GPIO83/82) are muxed to i2c3 and P27/P28 (GPIO1/0) to i2c0 — GPIO
  writes do not reach those pins; bsp marks them `!` and requires `force`.
