# Contributing

Thanks for helping improve bsp-dev-loop. Bug reports, documentation fixes, new SoC packs and code
contributions are all welcome.

## Development setup

```bash
git clone --recurse-submodules https://github.com/rzbdz/bsp-remote-dev-workflow.git bsp-dev-loop
cd bsp-dev-loop
python3 -m venv .venv
.venv/bin/pip install -e '.[server,dev]'
.venv/bin/pytest
```

The tests use the fictional site in `examples/site/` and a mock GPIO backend; they need no
hardware and no network. To try the service locally:

```bash
BSP_SITE=examples/site .venv/bin/bsp-hostd -c examples/site/hosts/dev-mock.yaml
```

## What goes where

bsp separates mechanism from knowledge about a particular installation:

| Belongs in | Examples |
|---|---|
| Core (`src/`, `hooks/`) | New step kinds, control or GPIO backends, CLI commands, generic hooks |
| A pack (`packs/<vendor>/`) | Facts anyone with the hardware can observe or download: SoC boot-ROM fingerprints and download-mode signatures, header pin maps of purchasable boards, hooks for documented image formats |
| A site (not in this repository) | Host names and addresses, accounts, build servers, SDK paths and layouts, boot and flash recipes for your trees, probe images you may not redistribute, boards on your bench |

Pull requests must not contain credentials, internal host names or addresses, personal paths, or
other installation-specific data. Use the documentation address ranges (`192.0.2.0/24`) and
made-up names like those in `examples/site/` in examples and tests.

## Pull requests

- Keep changes focused, and add or update tests for behaviour changes (`tests/`).
- Run `pytest` before submitting; CI runs it on every pull request.
- Update the documentation in `docs/` and `CHANGELOG.md` when you change user-facing behaviour.
- Commands should keep the house style: one action per command, `--json` output, and errors with
  a `hint` that names the next step.
- Hardware-facing changes: describe which hardware you tested on.

## Reporting bugs

Open an issue with the bsp version (`bsp hosts` shows it per host), what you ran, the output with
`--json`, and the relevant `bsp-hostd` log (`journalctl -u bsp-hostd`). Remove host names,
addresses and tokens before posting. Security issues: see [SECURITY.md](SECURITY.md).

By contributing you agree that your contributions are licensed under the Apache License 2.0.
