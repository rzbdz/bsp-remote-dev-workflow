#!/usr/bin/env bash
# Run the clean-agent end-to-end test. See README.md.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
STAMP=$(date +%Y%m%d-%H%M%S)
MARKER=${MARKER:-BSPE2E-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')}
TASK=${TASK:-e2e-$STAMP}
SDK=${SDK:?set SDK=<sdk id> (see bsp sdk list)}
export PATH="$REPO/bin:$PATH"

echo "marker=$MARKER task=$TASK sdk=$SDK"
SOC=$(bsp --json sdk show "$SDK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["soc"] or "")')
SOC_NAME=${SOC_NAME:-$(echo "$SOC" | tr a-z A-Z)}
OUT="${E2E_RESULTS:-$HERE/results}/$STAMP-$SDK"; mkdir -p "$OUT"
sed -e "s/{{MARKER}}/$MARKER/g" -e "s/{{TASK}}/$TASK/g" -e "s/{{SDK}}/$SDK/g" -e "s/{{SOC_NAME}}/$SOC_NAME/g" \
    "$HERE/task.md" > "$OUT/prompt.md"
bsp --json sdk status "$SDK" > "$OUT/sdk-before.json"
bsp --json dut list > "$OUT/duts-before.json" || true

WS=$(mktemp -d /tmp/bsp-e2e-ws-XXXX)
mkdir -p "$WS/.claude/skills"
cp -r "$REPO/skills/bsp-dev-loop" "$WS/.claude/skills/"
echo "workspace: $WS (only .claude/skills/bsp-dev-loop)"

START=$(date +%s)
( cd "$WS" && claude -p "$(cat "$OUT/prompt.md")" \
    --setting-sources project --no-session-persistence \
    --allowedTools "Bash,Read,Write,Edit,Glob,Grep" --permission-mode acceptEdits \
    --output-format stream-json --verbose < /dev/null ) > "$OUT/transcript.jsonl" 2> "$OUT/claude.stderr" || true
echo "agent finished after $(( $(date +%s) - START ))s"
python3 - "$OUT/transcript.jsonl" > "$OUT/answer.md" <<'PY'
import json, sys
final = ""
for line in open(sys.argv[1]):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") == "result":
        final = ev.get("result") or ""
print(final)
PY
echo "---- agent answer (tail) ----"; tail -5 "$OUT/answer.md"
"$HERE/verify.sh" "$MARKER" "$TASK" "$SDK" "$OUT/sdk-before.json" | tee "$OUT/verify.txt"
