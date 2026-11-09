#!/usr/bin/env bash
# Install the bsp skills for the agents used on a machine.
#
#   deploy/install-skills.sh [user@host|local]      default: local
#
# Copies skills/* into every agent skill dir that exists (or ~/.claude/skills):
#   Claude Code  ~/.claude/skills/<name>/SKILL.md
#   opencode     ~/.config/opencode/skill/<name>/SKILL.md
#   hermes       ~/.hermes/skills/embedded/<name>/SKILL.md
# The skills only reference the `bsp` command, so install the client too
# (deploy/install-client.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET=${1:-local}
INSTALL='set -e; T=$(mktemp -d); tar -xzf - -C $T;
for d in ~/.claude/skills ~/.config/opencode/skill ~/.hermes/skills/embedded; do
  [ -d "$(dirname $d)" ] || continue
  mkdir -p $d
  for s in $T/skills/*/; do n=$(basename $s); rm -rf $d/$n; cp -r $s $d/$n; echo "installed $n -> $d/$n"; done
done; rm -rf $T'
if [[ $TARGET == local ]]; then
  tar czf - skills | bash -c "$INSTALL"
else
  tar czf - skills | ssh -o BatchMode=yes "$TARGET" "$INSTALL"
fi
