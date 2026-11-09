# Getting started

This guide takes you from a fresh clone to booting your own build on a real board. It assumes
the roles described in the [README](../README.md#how-it-works): a **board host** wired to the
boards, a **build server** holding the SDK trees, and a **client** where you run `bsp`. One
machine can play several roles.

If you only want to look around, follow [Try it without hardware](../README.md#try-it-without-hardware)
instead.

## 1. Clone

```bash
git clone --recurse-submodules https://github.com/rzbdz/bsp-remote-dev-workflow.git bsp-dev-loop
cd bsp-dev-loop
```

The `third_party/serial-mux` submodule provides the serial consoles. If you cloned without
`--recurse-submodules`, run `git submodule update --init`; a host deployed without it has no
console support (the installer warns about this).

## 2. Create a site

All installation-specific data lives in a *site* directory outside this repository. Start from
the example and keep it under version control privately:

```bash
cp -r examples/site ~/bsp-site
cd ~/bsp-site && git init && cd -
export BSP_SITE=~/bsp-site
```

Keep the site repository at a location of your choice and point `BSP_SITE` at it.
`~/.config/bsp/site` is where `install-client.sh --site` installs *copies* of the site on other
machines; the script refuses to overwrite a git checkout or symlink there.

The minimum useful site describes one board host. Replace the example values in:

| File | What to set |
|---|---|
| `hosts/HOST.yaml` | Host id, `server.bind` and `server.auth_token` (see [SECURITY.md](../SECURITY.md)), the GPIO header (`modules.gpio_relay.board`, `backend`), and `modules.dut` |
| `deploy/HOST.env` | How to reach the host over ssh (`SSH_HOST`, `SSH_USER`), `INSTALL_DIR`, `SERVICE_PORT` |
| `remotes.json` | How clients reach the host: `url`, optional `fallback_urls`, and `ssh` for tunnelling |
| `boards/*.yaml` | The pin map of the host's GPIO header, if no pack provides it |

`HOST` must be the same in all three places. Delete the example SDK files you do not need; you
can add SDKs later (step 6). Every file format is described in
[configuration.md](configuration.md).

## 3. Deploy the board host

```bash
deploy/deploy.sh HOST             # uses $BSP_SITE/deploy/HOST.env
deploy/deploy.sh HOST status      # service state and health check
```

`deploy.sh` ships the committed bsp tree and the site to the host, installs a virtual
environment, registers the `bsp-hostd` systemd service and checks that it answers. Commit your
bsp changes first, or pass `--allow-dirty`. Use ssh keys; set `SSHPASS` only for hosts that
accept nothing but a password. See [deployment.md](deployment.md) for all options.

## 4. Install the client

On every machine you work from (workstation or build server):

```bash
deploy/install-client.sh USER@MACHINE --site ~/bsp-site    # or: deploy/install-client.sh local --site ~/bsp-site
```

This copies bsp to `~/bsp-dev-loop`, links `~/.local/bin/bsp` (make sure it is on your
`PATH`) and installs the site at `~/.config/bsp/site`. It logs in with ssh keys only. Then:

```console
$ bsp init
config dir: /home/dev/.config/bsp
mode: remote (talking to remote hosts)
  host lab-1            via http://192.0.2.20:8765  duts: -
next: `bsp probe` to discover boards, `bsp dut list`, `bsp sdk list`
```

## 5. Register boards

### Option A: probe (boards wired to GPIO reset lines)

`bsp probe` pulses candidate GPIO lines one at a time and watches which console and USB port
react, so it **drives pins on the header**. Without `--pins` the candidates are every header GPIO
that is free, not protected and not muxed to another function; pins already configured as
`button` or `relay` channels are tried first, raw `gpio` pins are pulsed low. Before the first
probe:

- list pins that must never be driven under `protected:` in the host config;
- run `bsp probe --dry-run` to see the candidates without touching anything;
- on any host where other header lines drive something, pass the reset lines explicitly with
  `--pins`, preferably configured as `button` channels (open drain).

```bash
bsp probe --dry-run
bsp probe --pins P33,P11          # interactive: review and name each board
```

Probe identifies the SoC from its boot-ROM output and, if the SoC profile provides one, boots a
known-good bootloader to read the board's identity. Rename boards with
`bsp dut rename OLD NEW`. To keep them in the site, run `bsp dut export --dir $BSP_SITE/duts`
(one file per board), remove the registered copies with `bsp dut remove`, and redeploy the host
so it loads the files. Boards can also simply stay registered on the host.

### Option B: bind (no GPIO wiring)

For a quick setup — batch flashing, or debugging together with a colleague — bind a board from
its console and USB port:

```bash
bsp dut bind slot-1 --console /dev/ttyUSB0 --usb usb:1-1.2 --soc k3
```

Find the USB path with `fastboot devices -l` while the board is in download mode. Whenever a
reset is needed, bsp asks a person to press the button and waits; see
[Boards reset by a person](cli.md#boards-reset-by-a-person).

## 6. Describe an SDK and boot it

Give the board host ssh access to the build server so it can fetch build artifacts:

```bash
deploy/grant-build-access.sh HOST USER@BUILD_SERVER
```

Then describe the build server and the SDK tree in the site (`sdk/servers/`, `sdk/instances/`,
and `sdk/families/` for the build style) — [sdk.md](sdk.md) explains each file and the boot
recipe steps. Check the result without touching hardware, then boot:

```bash
bsp sdk list
bsp sdk plan MY_SDK --boot load      # the steps a RAM boot would run
bsp sdk build MY_SDK kernel
bsp dut acquire BOARD
bsp dut boot BOARD --sdk MY_SDK
bsp dut console BOARD "uname -a"
bsp dut release BOARD
```

## Next steps

- Set up AI agents: [agents.md](agents.md).
- Learn every command: [cli.md](cli.md).
- Add a SoC or a board host type: [porting.md](porting.md).
