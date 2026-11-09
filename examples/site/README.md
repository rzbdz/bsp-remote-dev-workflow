# Example site

A complete, **made-up** site configuration showing every file format bsp reads. Real
sites live outside the bsp repo (`$BSP_SITE`, `~/.config/bsp/site` or `/etc/bsp/site`),
usually in a private git repo of their own. Copy this directory and replace every value.
Addresses use the documentation range 192.0.2.0/24. The test suite runs against it.

| path | read by | what |
|---|---|---|
| `bsp.yaml` | all | site settings: which packs to use |
| `remotes.json` | CLI | board hosts: url, fallback urls, ssh target |
| `hosts/<id>.yaml` | bsp-hostd | modules of one board host |
| `deploy/<id>.env` | `deploy/deploy.sh <id>` | how to reach and install a host |
| `boards/*.yaml` | gpio_relay | header pin maps of host controller boards |
| `sdk/families/*.yaml` | CLI + hosts | build styles (make targets, env script + function, tree script) |
| `sdk/servers/*.yaml` | CLI | build servers (ssh, path aliases, job environment) |
| `sdk/workspaces/*.yaml` | CLI | source trees shared by several SDK targets (unit of locking) |
| `sdk/instances/*.yaml` | CLI + hosts | SDK targets: workspace, family, SoC, artifacts, boot recipes |
| `socs/<soc>/soc.yaml` | hosts | boot-ROM fingerprint, download mode, probe images, identity patterns |
| `duts/*.yaml` | hosts | static boards (probed ones live in the host state dir) |
| `hooks/<kind>/<name>` | hosts | site hooks, e.g. a vendor flash tool wrapper |
| `.bspignore` | `deploy.sh`, `install-client.sh` | tar exclude patterns: site files not to ship to hosts / clients |
| `hints/*.md` | `bsp hints`, `bsp install` | free text for agents and people (credentials conventions, lab rules); added to generated SKILL.md files |
