# e2e-material — end-to-end acceptance test of the bsp dev loop

Proves that an agent with **no context** except the `bsp-dev-loop` skill and the `bsp`
binary can run the whole loop on real hardware:

change SDK code -> build on the build server -> lease a board on a board host ->
reset into download mode -> RAM boot -> `dmesg | grep` the new print on the serial console
-> commit on the task branch -> release the board and the SDK (owner's work restored).

| file | role |
|---|---|
| `task.md` | the only instruction the agent gets (`{{MARKER}}`, `{{TASK}}` filled in per run) |
| `run-e2e.sh` | snapshots the SDK state, starts a clean `claude -p` session in an empty workspace whose only project content is `.claude/skills/bsp-dev-loop`, with `bsp` on PATH; then runs `verify.sh` |
| `verify.sh` | independent checks with `bsp --json`: marker in the board's console log, commit on `bsp/<task>` contains it, SDK + DUT leases released, owner's branch/dirty files restored |
| `results/<stamp>-<sdk>/` | prompt, agent transcript (stream-json), final answer, verify report (not committed) |

Run: `SDK=<sdk> e2e-material/run-e2e.sh` (needs: `claude` logged in on this machine, ssh key
access to the build server, a reachable board host with a free board of the SDK's SoC).
Override `MARKER`, `TASK`, `SOC_NAME` (used in the prompt; default: the SoC id) and
`E2E_RESULTS` (default `e2e-material/results/`, git-ignored — keep run records in your site
repo, they contain host names and console logs). The agent session is isolated with
`--setting-sources project` and a fresh temp workspace, so no user CLAUDE.md, memory or other
skills leak in.

Each result dir has `prompt.md`, `transcript.jsonl` (full stream-json), `steps.md` (tool calls),
`answer.md` and `verify.txt`. A run passes when `verify.txt` shows 5/5 PASS.
