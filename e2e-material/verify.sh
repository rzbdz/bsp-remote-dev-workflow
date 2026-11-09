#!/usr/bin/env bash
# Independent verification of an e2e run: verify.sh MARKER TASK SDK [sdk-before.json]
set -uo pipefail
MARKER=$1; TASK=$2; SDK=${3:?usage: verify.sh MARKER TASK SDK [sdk-before.json]}; BEFORE=${4:-}
fails=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails+1)); }

# 1. the marker was printed by a booted kernel (passive console log, no lease needed)
hit=""
for d in $(bsp --json dut list | python3 -c 'import json,sys; print(" ".join(x["id"] for x in json.load(sys.stdin)["result"]))'); do
  # a kernel log line has a dmesg timestamp "[    0.055945]"; this excludes the echoed grep command
  line=$(bsp dut log "$d" --lines 4000 --grep "\[ *[0-9]+\.[0-9]{6}\] [^|]*$MARKER" 2>/dev/null | tail -1)
  [[ -n $line ]] && { hit="$d: $line"; break; }
done
[[ -n $hit ]] && pass "kernel printed the marker: $hit" || fail "no dmesg line with $MARKER in any DUT console log"

# 2. the change is committed on the task branch
if bsp sdk exec "$SDK" --read-only -- git show "bsp/$TASK" 2>/dev/null | grep -q "$MARKER"; then
  pass "commit on bsp/$TASK contains the marker"
else
  fail "branch bsp/$TASK has no commit containing $MARKER"
fi

# 3. leases released
sdk_lease=$(bsp --json sdk status "$SDK" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["lease"])')
[[ $sdk_lease == None ]] && pass "SDK $SDK released" || fail "SDK $SDK still leased: $sdk_lease"
held=$(bsp --json dut list | python3 -c 'import json,sys; print(",".join(d["id"] for d in json.load(sys.stdin)["result"] if d["lease"]))')
[[ -z $held ]] && pass "no DUT left leased" || fail "DUTs still leased: $held"

# 4. the owner's state is back (same branch/head and same dirty files as before the run)
if [[ -n $BEFORE && -f $BEFORE ]]; then
  bsp --json sdk status "$SDK" > /tmp/bsp-sdk-after.json
  python3 - "$BEFORE" /tmp/bsp-sdk-after.json <<'PY' && pass "owner's branch, head and dirty files restored" || fail "SDK working state differs from before the run"
import json, sys
def key(p):
    r = json.load(open(p))["result"]
    return [(x["project"], x["branch"], x["head"], sorted(x["dirty_files"])) for x in r["projects"]]
a, b = key(sys.argv[1]), key(sys.argv[2])
if a != b:
    print("  before:", a); print("  after :", b); sys.exit(1)
PY
fi
echo
[[ $fails == 0 ]] && echo "E2E PASSED" || echo "E2E FAILED ($fails check(s))"
exit $fails
