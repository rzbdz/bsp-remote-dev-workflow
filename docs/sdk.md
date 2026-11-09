# SDKs and boot recipes

An **SDK target** is something you can build and boot: a source tree on a build server, a build
style, the SoC it is for, where its build artifacts land, and how to put them on a board. All of
it is site configuration under `SITE/sdk/`.

```text
sdk/servers/<id>.yaml       build servers: ssh login and job environment
sdk/families/<id>.yaml      build styles: how to build a component, where artifacts land
sdk/workspaces/<id>.yaml    source trees shared by several targets (optional)
sdk/instances/<id>.yaml     SDK targets: what you name in `bsp sdk …` and `bsp dut boot --sdk`
```

Validate a change without hardware with `bsp sdk list`, `bsp sdk show SDK` and
`bsp sdk plan SDK [--boot load|download]`.

## Build servers

```yaml
id: build-a
host: 192.0.2.10
user: dev
description: shared build machine
home_aliases: [/home/dev, /data/home/dev]   # the same home under two paths (used by `bsp install`)
env:                                        # what an interactive login would set up, for build jobs
  path_prepend: ["~/toolchains/riscv/bin"]
  vars: {LD_LIBRARY_PATH: "~/.local/lib"}
```

bsp runs a small stdlib-only helper over ssh (Python ≥ 3.6); nothing is installed on the server.

## Families (build styles)

```yaml
id: make-bsp
build:
  runner: make                 # make | envsetup | script
  targets: {kernel: kernel, uboot: uboot, all: all}      # component -> make target
  clean: {kernel: clean_kernel}
artifacts:
  dir: output                  # relative to the tree; may contain {plat}
  roles:                       # role -> file name, may contain {plat} (globs pick the newest match)
    fsbl: FSBL.bin
    uboot-sbi: u-boot-opensbi.itb
    kernel-fit: Image.itb
dev_projects: [linux]          # git repositories inside the tree that a lease branches
env: {path_prepend: [], vars: {}}   # added to the build server's job environment
console: {prompt: "# $"}       # shell prompt on the booted board
```

| Runner | Build command for component `C` |
|---|---|
| `make` | `cd TREE && make TARGETS[C]` |
| `envsetup` | `cd TREE && source ENVSETUP PLAT && TARGETS[C]` — `build.envsetup` names the script, the instance's `plat:` the platform, the target a shell function it defines |
| `script` | `cd TREE && TARGETS[C]`, with `{field}` replaced from the instance (e.g. `./build.sh -b {board}`) |

## Workspaces and leases on source trees

A **workspace** is one checkout on one server and the unit of mutual exclusion. Several targets
can share it — for example one upstream kernel tree built for two SoCs. A target without
`workspace:` owns an implicit workspace (its server and path).

```yaml
id: kernel-tree
server: build-a
path: /home/dev/work/kernel-tree
dev_projects: ["."]
```

What `bsp sdk acquire SDK --task NAME` does in each dev repository:

1. records the current branch and commit;
2. stashes uncommitted changes to tracked files (`git stash push`; untracked files stay);
3. checks out `bsp/NAME`, creating it from the current commit, or resumes it if it exists.

`bsp sdk release SDK` stashes whatever is still uncommitted on the task branch, checks out the
recorded branch and pops the stash from step 2. The task branch and its commits are kept.
Commit your work before releasing.

Lease state lives in the tree, in `TREE/.bsp/`:

| Path | Contents |
|---|---|
| `.bsp/lease.json` | Holder, task, branch, recorded state per repo, token, expiry (default 8 h) |
| `.bsp/jobs/<id>/` | Build job script, log, exit code, metadata |
| `.bsp/build.lock` | One build per workspace at a time |

bsp adds `/.bsp/` to `.git/info/exclude` when the tree root is a repository. If a lease was lost
(for example the token is gone), the holder information in `lease.json` tells you who had it;
`bsp sdk release SDK --force` releases it and restores the recorded state (`--keep` releases without switching branches).

## SDK targets

```yaml
id: bsp-a
family: make-bsp
server: build-a                 # or `workspace: <id>` to share a tree
path: /home/dev/work/bsp-a
soc: soc-a                      # boards of this SoC can run it
description: vendor BSP for soc-a
boot:
  load:                         # RAM boot; nothing is written to storage
    status: ready               # ready | todo
    steps: [...]
    variants: {uboot-only: stop once U-Boot answers fastboot}
  download:                     # flash to storage
    status: todo
    notes: "flash layout not described yet"
    steps: []
```

### Overriding the family per target

| Key | Effect |
|---|---|
| `build.targets` | Extra or replaced component → target entries (merged over the family's) |
| `build.pre` | Per component, a shell command run in the tree before the build target |
| `artifacts` | Extra or replaced role → file name entries |
| `artifacts_dir` | Replaces the family's `artifacts.dir` |
| `plat` | Platform name: argument of the envsetup script and `{plat}` in artifact paths |
| `dev_projects`, `env`, `console` | Replace the family's value; a shared workspace's value wins over both |
| any other field | Available as `{field}` in `script` runner targets (e.g. `board:` for `./build.sh -b {board}`) |

Both `load` and `download` must be present; an unfinished one is `status: todo` with `notes`,
which `bsp dut boot` shows instead of running anything.

## Boot recipe steps

Steps run in order on the board host. Each is a mapping with `do:` and its options.

| Step | Options | Effect |
|---|---|---|
| `enter-fdl` | `timeout` | Reset into download mode through the board's control backend, then wait until the SoC profile's download-mode console line and fastboot device appear |
| `reset` | | Plain reset |
| `stage` | `artifact`, `timeout`, `stop_if` | `fastboot stage` the artifact with that role |
| `continue` | | `fastboot continue` |
| `oem` | `cmd` | `fastboot oem CMD` |
| `getvar` | `name`, `match` | `fastboot getvar NAME`; fail unless it matches the regex |
| `flash` | `partition`, `artifact`, `timeout` | `fastboot flash PARTITION FILE` |
| `wait` | `serial`, `fastboot`, `fail`, `timeout`, `stall`, `nudge` | Wait for a console regex and/or a fastboot product regex. `fail`: regex that aborts; `stall`: seconds of console silence that count as failure; `nudge`: press Enter after that many quiet seconds (for prompts printed without a newline — not while a bootloader may treat a key as "stop autoboot") |
| `settle` | `seconds`, `why` | Fixed delay; state why |
| `hook` | `kind`, `name`, `artifacts`, `args`, `timeout` | Run `hooks/KIND/NAME` (e.g. a vendor flash tool); see [hooks/README.md](../hooks/README.md) |
| `check` | `serial`, `why` | The console since the last action (a reset, a `continue`) matches a regex; otherwise stop |
| `boot-media` | | Boot the board's own media: a plain reset, or its SoC's `media_boot` when FDL is strapped |

Common to every step:

- `when: fdl-not-strapped` — the step needs the board to boot its own media (the cold boot after
  flashing). On boards whose FDL is strapped a `reset` step goes through the SoC's `media_boot`
  ([porting.md](porting.md)); without one these steps are skipped and the log says why;
- `stop_if: VARIANT` — end the recipe here when that variant was requested
  (`bsp dut boot … --variant VARIANT`).

Before running, the host fetches every artifact the recipe refers to from the build server over
ssh and caches it by checksum, so unchanged files are not copied again.

## The catalog in the registry

With a host registry (see [configuration.md](configuration.md#host-registry)), the registry keeps
the SDK catalog — SDK instances with their `load` and `download` recipes, families, build servers
and workspaces — and hands it to every host and client. The site's `sdk/` directory stays as the
seed underneath: a document the registry does not have comes from the site, and deleting one in
the registry hides the site's copy as well. `bsp catalog import` takes the site's documents into
the registry once; `bsp catalog export --dir DIR` writes the registry's documents back as files
(a backup you can keep in git).

| Command | Purpose |
|---|---|
| `bsp catalog list [--kind sdk\|family\|server\|workspace]` | Documents, where each comes from, its revision and last change |
| `bsp catalog get REF [-o FILE]` | A document as YAML; `-o` writes it with a first line naming its revision |
| `bsp catalog put REF -f FILE [--base REV] [--force] [--check] [-m MSG]` | Write a document |
| `bsp catalog recipe SDK load\|download -f FILE [...]` | Replace one recipe: a list of steps, or `{status, steps, notes, variants}` |
| `bsp catalog rm REF --base REV [-m MSG]` | Delete a document |
| `bsp catalog history REF`, `bsp catalog rollback REF REV` | Every revision, and writing an earlier one again |
| `bsp catalog status` | The catalog revision, which revision each host has, and site edits it overrides |
| `bsp catalog refresh` | Clear this machine's cached catalog and have every host fetch it again now |

`REF` is an SDK id, or `family/ID`, `server/ID`, `workspace/ID`.

Anyone may change the catalog; what keeps it safe:

- **No lost updates.** A change names the revision it was based on (`put` reads it from the first
  line `get -o` wrote). If someone changed the document meanwhile, the write is refused with who
  did it; get it again and redo the change, or pass `--force`.
- **Validated first.** The whole catalog is loaded with the change applied and every `ready`
  recipe is planned; a change that breaks any SDK (an unknown server, artifact role or step) is
  refused with the reason, and nothing is stored. `--check` only validates.
- **Everything is kept.** Each write is a new revision with its author (`--author`, default
  `$BSP_HOLDER` or `$USER`) and message; `rollback` restores any earlier one.

Hosts learn the catalog revision from the registry's answer to their announcements, and the
registry nudges them right after a change. A host fetches the new catalog into `<state>/catalog`,
reloads it without restarting, and keeps it across restarts and outages. Boots already running
keep the recipe they started with; each boot job's log names the catalog revision its recipe came
from. Clients (`bsp sdk`, `bsp catalog`) read the registry's catalog too, keeping the last copy in
`~/.cache/bsp/` for when the registry cannot be reached. The registry stores the catalog in
`<state>/registry/catalog.sqlite`.

**Edit the registry, not a host's files.** Once the registry has a document, it overrides the
file of the same id in any site directory, so an edit made in a host's copy of the site
(`/opt/bsp-dev-loop/site/sdk/...`) has no effect. Hosts compare their site files with the
registry's documents and report every such edit as not in effect: in `bsp catalog status`, in
the host's `bsp status`, in its log, and in the log of any boot that uses the document. Publish
the edit with `bsp catalog put REF -f FILE` (after a `get` if someone changed it since).

### Keeping the catalog in git (git-registry)

The registry can keep the catalog in a git repository as well. Make the registry's site
directory a clone of the repository (the whole site: hosts, SoC profiles and so on can live
there) and set

```yaml
modules:
  registry:
    git: {remote: git@git.example.org:lab/bsp-catalog.git, branch: main, pull_s: 60}
```

- Every catalog write is committed to the clone (`sdk/<kind>/<id>.yaml`) with the author and
  message of the change and a `Catalog-Revision:` trailer, and pushed, in order, by a background
  worker. If the remote moved, it takes those commits in first and pushes again.
- Commits pushed to the repository by anyone else are pulled every `pull_s` seconds, or at once
  with `bsp catalog git_pull` (host command on the registry). Each SDK document they change is
  validated against the whole catalog and applied as `git:<commit author>`; hosts get it like
  any other change. A document that does not validate is not applied: the registry keeps the
  version before it and commits that back, which reverts the change in the repository with the
  reason, and `bsp catalog status` lists it.
- The registry needs push access (its ssh key on the repository). `bsp catalog status` shows the
  repository, the head, what was last pushed, writes still pending and the last error.
