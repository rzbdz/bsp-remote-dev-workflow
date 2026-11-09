# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) from 1.0 on.

## [Unreleased]

### Added
- Slots (`bsp slot list|status|console|pipe|scan|forget`): every serial console on a host is a slot
  with a serial-mux console of its own, usable before any board is registered. Boards, reset lines
  and download ports are attached to slots; removing a board leaves its slot and its history.
  Any DUT can be named by its slot (`<host>/s3`).
- `bsp dut refresh`: re-read the board in a slot after swapping it (identify, update board, SoC,
  model, and the download port if its cable moved), keeping the lease; `--rename` names it after
  what identify read.
- `bsp probe --register` registers each board the moment it is found; `--slots` probes only some
  slots; `--known` only tries reset lines slots had before. Hosts tell the registry about new
  boards and slots at once.

- Media boot: a board whose FDL strap is held down can boot its own media when its SoC profile (or
  the DUT) declares `media_boot` steps, run from download mode. A plain reset
  (`bsp dut reset --no-fdl`) and recipe steps marked `when: fdl-not-strapped` use them; `bsp dut
  status` shows it. New recipe steps: `check` (the console matches, else stop) and `boot-media`.
- `bsp images list|sync`: SoC images a site lists with a `source` and `sha256` are fetched once into
  `<site>/.blobs/<sha256>/`; `deploy.sh` syncs them before shipping the site.

### Changed
- Probe tries the reset lines slots had before first (`probe.pin_order`), presses a line that made
  several consoles speak again (`probe.retry_ambiguous`), skips consoles whose adapter is gone,
  and says why a slot found nothing. `bsp probe --yes` registers each board as it is found.
- Interactive consoles (`bsp dut console`, console logins) leave with Ctrl-A Ctrl-X, as in picocom;
  Ctrl+] and Ctrl-C now go to the board (serial-mux's smtty reads picocom's keys).

### Fixed
- Probe no longer says a SoC has no identify section when it skipped identify because no download
  port appeared.
- The SpacemiT flash hook skips a medium U-Boot reports as `NULL` (no SPI NOR on a UFS board)
  instead of flashing a NOR layout there and failing after the block layout was written.
- Boot and flash recipes no longer fail on a board that is already in download mode: either the
  boot ROM's console banner or its USB download device now counts as being in download mode.

### Added
- git-registry (`modules.registry.git`): the registry commits every catalog change to a git
  repository and validates commits pushed there by others before taking them in.
- `bsp catalog refresh`; hosts report site edits the registry's catalog overrides (not in effect).
- The SDK catalog in the registry (`bsp catalog …`): versioned documents with history and rollback,
  conflict checks, validation of every change against the whole catalog, distribution to hosts
  without restarts; the site's `sdk/` is the seed.
- Hosts watch for serial and USB devices coming and going (`dut.ports`) and tell the registry at
  once (`registry.ports`); `bsp watch --registry [--json]` streams the registry's events.
- `bsp probe --human`: find boards without reset wiring by asking a person to put each one into
  download mode, pairing its console with the USB port that appears.
- `bsp dut renew`: extend a lease from now, keeping its token.
- `bsp dut pipe`: the board's serial-mux protocol on stdin/stdout, for programs; `dut status`
  reports `console_socket` and `console_up`.
- Artifact upload (`PUT /api/v1/artifacts/<sha256>`), streamed over HTTP and the ssh tunnel.
- `bsp dut stage` and `bsp dut present`: load a local file into RAM through the download port,
  and check whether the port is on the bus.
- `bsp dut boot --files`: boot from RAM with local files in order.
- Host registry: the `registry` module, `server.announce` on board hosts, and `"registry"` in
  `remotes.json` (or `$BSP_REGISTRY`) so clients find announced hosts.

## [0.1.0b0] - 2026-11-05

First public beta.

### Added
- Board leases, board registry and discovery (`bsp probe`), with SoC identification from
  boot-ROM output.
- Remote SDK workspaces on build servers: leases that park the owner's work on a task branch,
  detached builds, remote file access and commands.
- Boot recipes for RAM boot (`load`) and flashing (`download`), including hook steps for vendor
  flash tools and a `nudge` option for prompts printed without a newline.
- GPIO relay module with a web console: header pin map, channel modes, aliases, pin-mux checks.
- `bsp dut bind` and the `human` control backend: boards without reset wiring, reset by a person
  on request and resumed with `bsp dut confirm` or automatic download-mode detection.
- Console logins: `ssh bsp-<dut>@<host>` opens a board's console (opt-in).
- Multi-host CLI over HTTP or ssh tunnels, `--json` output and stateless mode for agents.
- Agent skills, `bsp install` and `bsp hints`; the end-to-end acceptance test in `e2e-material/`.
- Configuration layers: core, hardware packs (`packs/spacemit` for SpacemiT K1 and K3) and an
  external site directory; `examples/site/` documents every format.

[Unreleased]: https://github.com/rzbdz/bsp-remote-dev-workflow/compare/v0.1.0b0...HEAD
[0.1.0b0]: https://github.com/rzbdz/bsp-remote-dev-workflow/releases/tag/v0.1.0b0
