---
name: bsp-dev-loop
description: Develop, build and hardware-test BSP / kernel / bootloader code on real boards through the `bsp` CLI. Use when asked to change SDK code and verify it on a board, build an SDK, boot or flash a board (DUT), reset it, read its serial console or dmesg, find which boards and SDKs exist, manage boards, or extend bsp with a new SDK, board or procedure.
---

# bsp dev loop

> **Stay on the CLI.** If `bsp` commands do what you need, do not read bsp's source, configs or
> hooks, and do not ssh into board hosts or build servers to look around — it only costs time.
> Act on `error.hint` first. Dig into bsp internals only when a command keeps failing and its hint
> does not resolve it, or when the user asks you to extend bsp (see the last sections).

`bsp` is the only tool you need. It hides the network: SDK source trees live on build
servers, boards (DUTs) hang off board hosts. You never ssh anywhere yourself for the normal
loop and never touch GPIO, fastboot or serial devices directly. Names below in `<angle
brackets>` come from `bsp sdk list` / `bsp dut list` (or the header of a generated SKILL.md).

Use `--json` when you need to parse output: every command prints `{"ok": true, "result": ...}`
or `{"ok": false, "error": {"code", "message", "hint", "details"}}`. On failure **read
`error.hint` first** — it says what to do next. Exit codes: 0 ok, 1 failed, 2 usage, 3 host unreachable.

## 0. Look around (read-only, safe)

```bash
bsp hosts                        # reachable board hosts
bsp sdk list                     # SDK targets: SoC, workspace, boot methods load (RAM boot) / download (flash)
bsp sdk show <sdk>               # build server, path, dev repos, components, artifact files, workspace sharing
bsp sdk status <sdk>             # lease, current branch, dirty files, recent builds
bsp dut list                     # boards: host, SoC, stage, lease, which SDKs they run
bsp dut list --sdk <sdk> --free  # boards that can run an SDK and are free
```

An SDK target belongs to a **workspace** (one source tree on one build server). Several targets
can share a tree (one upstream kernel tree building for two SoCs): the lease is on the tree, so
leasing one leases all of them. Boards and SDKs have a SoC; a board only runs SDKs of its SoC.

## 1. Lease the SDK and get a task branch

```bash
bsp sdk acquire <sdk> --task <short-task-name> --holder <your-id>
```

This stashes the owner's local changes in the dev repos (listed by `bsp sdk show`) and checks
out a fresh branch `bsp/<task>`. Nobody else can build or write the tree while you hold it.
`bsp sdk release` later restores the owner's branch and stashed work.

## 2. Edit code (remotely)

Paths are relative to the SDK root (see `bsp sdk show`).

```bash
bsp sdk get <sdk> <repo>/init/main.c -o /tmp/main.c      # fetch a file
# ... edit /tmp/main.c locally ...
bsp sdk put <sdk> <repo>/init/main.c /tmp/main.c         # write it back
bsp sdk exec <sdk> -- grep -n "kernel_init" init/main.c  # any command; cwd = first dev repo
bsp sdk exec <sdk> -- git diff                           # review
bsp sdk exec <sdk> -- git commit -am "'my change'"       # commit on bsp/<task>
```

`exec` runs in the first dev repo (`--cwd` relative to the SDK root to change). Quote shell
metacharacters (`"'...'"`, `"&&"`, `"grep -E 'a|b' f"`): the command is re-joined on the build server.
If you already work inside the tree on the build server, edit files directly instead.

## 3. Build

```bash
bsp sdk build <sdk> <component>      # components: `bsp sdk show <sdk>`
```

Blocks until done, prints warnings/errors and a heartbeat; the exit code is the result. A first
build after acquire may recompile a lot; later ones are incremental. On failure the error carries
the log tail; full log: `bsp sdk job <sdk> <id> --log`.

## 4. Lease a board and boot your build

```bash
bsp dut acquire <dut> --holder <your-id> --purpose "<task>"
bsp dut boot <dut> --sdk <sdk>                    # --method load (default): RAM boot, nothing written
bsp dut boot <dut> --sdk <sdk> --method download  # flash to storage (only where `sdk list` says ready)
```

`boot` copies the SDK's artifacts to the board host, resets the board into download mode,
runs the SDK's boot recipe (stage / continue / flash steps) and waits until the console shows
the expected prompt. Progress lines are printed; the exit code is the result.

## 5. Check on the console

```bash
bsp dut console <dut> "dmesg | grep <your-marker>"
bsp dut console <dut> "cat /proc/version"
bsp dut log <dut> --grep <regex>          # passive: recent console lines, no keystrokes
```

One command per call; the console is the board's serial shell.

## Boards reset by a person (control backend `human`)

Some boards have no reset wiring: `bsp dut status` shows `control: {'backend': 'human', ...}`
(often slots made with `bsp dut bind`). On those, `bsp dut reset` and `bsp dut boot` stop and
ask a person to press RESET (or to enter download mode), and **block until it is done**:

1. Start them **in the background** (your tool's background mode, or `... &` with output to a
   file), never in the foreground — otherwise you are stuck until the person acts.
2. Tell the user exactly what the job printed after `ACTION NEEDED:` (e.g. "press RESET on
   slot-1"). `bsp dut list` shows such boards as stage `needs-human`.
3. When the user says it is done, run `bsp dut confirm <dut>` (optionally with a note). The job
   may also continue by itself when bsp sees the board in download mode on the console or USB.
4. Then read the background job's output for the result as usual.

If the user says the reset happened but the job then fails waiting for fastboot, the board did
not reach download mode: ask them to retry (hold the BOOT/download button if the board has one).

## 6. Clean up (always, also after failures)

```bash
bsp dut release <dut>
bsp sdk release <sdk>        # parks uncommitted work as a stash, restores the owner's state;
                             # the branch bsp/<task> (with your commits) is kept
```

## Rules

- Commit your change on the task branch before `sdk release`, otherwise it ends up in a stash.
- If a board or SDK is leased by someone else (`error.code` conflict), pick another or wait; never `--force`.
- Leases are remembered per user; parallel agents on one machine should pass `--stateless`
  and `--lease <token>` (or `BSP_LEASE` / `BSP_SDK_LEASE`).
- `bsp dut reset <dut>` resets into download mode; `bsp dut status <dut>` shows the stage.
- `bsp install` (in an SDK directory) writes ./SKILL.md: this skill plus which SDK the directory is
  and the site's hints.
- Everything has `--help`: `bsp dut boot --help`, `bsp sdk build --help`.

## Consoles for people

`bsp dut console <dut>` without a command attaches interactively (Ctrl-A Ctrl-X leaves, Ctrl-C goes to the board; needs a
terminal and ssh access to the host). Hosts with console logins enabled also accept
`ssh bsp-<dut>@<host>` with no password; `bsp dut logins` prints the exact lines. Give these to
the user rather than running them yourself.

## Managing boards (when the user asks)

Every serial console on a host is a slot (`bsp slot list`): a board sits in a slot, and any `<dut>`
can be named by its slot instead (`<host>/s3`). That name keeps working when someone swaps the
board in the slot, so for repeated runs on "whatever is in s3" use the slot name. A slot without a
board still has a console: `bsp slot console <host>/s3` (or `pipe`) to watch it while wiring or debugging.

`bsp probe --yes` discovers new boards on free slots (`-r <host>` for one host, `--slots s3` for
some): reset line, console, USB port, SoC (from its boot ROM) and identity; each board is
registered the moment it is found. After re-cabling, `bsp probe --yes --known` only tries the reset
lines the slots had before. When the board in a slot was swapped for another, do not remove and
re-add it: `bsp dut refresh <host>/s3 [--rename | --name <new>]` (with its lease) reads the new
board and keeps the slot, the lease and the reset line. Then `bsp dut rename <old> <new> [--console]`,
`bsp dut set <dut> --description/--sdks/--soc/--reset-pin/--usb/--fdl ...`, `bsp dut remove <dut>`,
`bsp dut export`. Only touch boards you hold or that are free. For a quick setup without GPIO
wiring (batch flashing, debugging together with a person), bind a slot from a console and a USB
port: `bsp dut bind <name> --console /dev/ttyUSB0 --usb usb:1-1.2 --soc <soc> [--host <host>]`;
its resets are done by a person (see above). `bsp dut remove <name>` drops it again. To keep probed boards in the site
configuration, save `bsp dut export` output under `<site>/duts/`.

## Debugging on a host (only when bsp cannot tell you enough)

Board hosts are reachable over ssh as listed in `<site>/remotes.json` (`ssh` field). Useful there:
`journalctl -u bsp-hostd -n 100`, `fastboot devices -l`, `serial-mux list`,
`systemctl status bsp-console-<console>`, `~/.serial-mux/logs/<console>/`, `/var/lib/bsp/`. Try
`bsp dut status`, `bsp dut log` and `bsp events recent -r <host>` first; never drive lines or
fastboot by hand while bsp holds the board.

## Extending bsp (contribute when a flow is missing)

bsp is mechanism only. What is specific to this installation lives in the **site** directory
(`$BSP_SITE`, else `~/.config/bsp/site`, else `/etc/bsp/site`; `examples/site/` in the bsp repo
shows every format). Public hardware knowledge lives in **packs** (`<bsp repo>/packs/<vendor>/`).
When the user asks you to bring up something bsp cannot do yet, add it there instead of working
around it:

- SDK target: `<site>/sdk/instances/<id>.yaml` — copy the closest existing one. Shared trees:
  `sdk/workspaces/`, build styles: `sdk/families/`, build-server env: `sdk/servers/`. Fill
  `boot.load` / `boot.download` steps (step kinds: `bsp/sdk/catalog.py`); unknown parts stay
  `status: todo` with `notes`.
- Procedures that are more than steps (e.g. a vendor flash tool): an executable in
  `<site>/hooks/<kind>/<name>` (contract: `hooks/README.md` in the bsp repo), used by a
  `{do: hook}` step. Public formats any user of that hardware needs belong in a pack.
- New SoC: `socs/<soc>/soc.yaml` (boot-ROM fingerprint, download mode, identity patterns) in a
  pack; probe images you built go in `<site>/socs/<soc>/`.
- New board host: `<site>/hosts/<id>.yaml`, `<site>/deploy/<id>.env`, an entry in
  `<site>/remotes.json`, then `deploy/deploy.sh <id>` from the bsp repo (ask the user first; it
  restarts the host service).
- Check: `bsp sdk plan <sdk> --boot load`, then a real `bsp dut boot`; run the tests if you changed
  code. Commit site changes in the site repo and code changes in the bsp repo, with clear
  messages; push only if the user asks. Never put credentials or site details into the bsp repo.
