# Using bsp with AI agents

bsp was designed so a coding agent can run the whole hardware loop with nothing but a shell: no
ssh sessions to manage, no device paths to discover, and errors that say what to do next.

## Give the agent the skill

The repository ships two skills:

| Skill | For |
|---|---|
| `skills/bsp-dev-loop` | The full loop: SDK leases, remote edits, builds, board leases, boot, console checks, cleanup; managing boards; extending bsp |
| `skills/bsp-gpio-relay` | Direct GPIO, relay and button control on a board host |

Two ways to install them:

- **Globally** for the agents on a machine: `deploy/install-skills.sh [USER@MACHINE]` copies
  them into `~/.claude/skills`, `~/.config/opencode/skill` and `~/.hermes/skills/embedded`
  (whichever agent directories exist).
- **Per SDK tree:** run `bsp install` in the tree (on the machine where the agent works). It
  writes `./SKILL.md`: a header describing which configured SDK target this directory is, which
  boards can run it, the site's hints, and then the full `bsp-dev-loop` skill. An existing
  `SKILL.md` is backed up as `SKILL.md.bak.<timestamp>`. The directory is matched against the
  SDK paths in the site (symlinks and the build server's `home_aliases` are resolved); for an
  unknown directory the header tells the agent to ask the user. Point your agent at this file
  the way it expects (for Claude Code, for example, copy or link it to
  `.claude/skills/bsp-dev-loop/SKILL.md`, or reference it from `CLAUDE.md`).

## Conventions agents should follow

- Use `--json` and read `error.hint` first when something fails.
- Several agents on one machine: pass `--stateless` (or set `BSP_STATELESS=1`), name the host
  with `-r` where it matters, and pass lease tokens explicitly (`--lease`, or `BSP_LEASE` /
  `BSP_SDK_LEASE`). Otherwise leases are remembered per user in `~/.config/bsp/leases.json`.
- On boards reset by a person (control backend `human`), run `bsp dut reset` and `bsp dut boot`
  in the background, relay the `ACTION NEEDED` line to the user, and run
  `bsp dut confirm DUT` when the user says it is done.
- Release every lease at the end, also after failures.

## Tools over HTTP

Each board host describes its commands for programmatic use:

| Endpoint | Contents |
|---|---|
| `GET /llms.txt` | A plain-text overview of the host's commands |
| `GET /api/v1/tools?format=anthropic` | Tool definitions in Anthropic format |
| `GET /api/v1/tools?format=openai` | Tool definitions in OpenAI format |
| `POST /api/v1/tools/NAME` | Call a tool with a JSON body of its arguments |
| `GET /docs` | OpenAPI documentation |

SDK operations that run over ssh on build servers (acquire, build, exec, …) are not host tools;
agents use the CLI for them. An MCP server is planned.

## Acceptance test

`e2e-material/` checks that the skill is sufficient on its own. `run-e2e.sh` starts a fresh agent
session (Claude Code, isolated from user settings, memory and other skills) in an empty
directory that contains only the skill, and asks it to add a kernel log line, build, boot a free
board, prove the line appears in `dmesg`, commit and release everything. `verify.sh` then checks
the outcome independently. See [e2e-material/README.md](../e2e-material/README.md).
