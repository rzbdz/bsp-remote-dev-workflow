#!/usr/bin/env bash
# Let a board host pull build artifacts from build servers over ssh.
#
#   SSHPASS=... deploy/grant-build-access.sh <host-target> <user@build-server>...
#
# Creates a key for SSH_USER (root: the user bsp-hostd runs as) on the board host if needed, then appends it to each build
# server's authorized_keys (this machine must already have ssh access to them).
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET=${1:?usage: deploy/grant-build-access.sh <host-target> <user@build-server>...}
shift
SITE=${BSP_SITE:-}
for c in "$SITE" "$HOME/.config/bsp/site" /etc/bsp/site; do [[ -n $c && -d $c ]] && { SITE=$c; break; }; done
[[ -f $SITE/deploy/$TARGET.env ]] || { echo "no deploy target $TARGET in the site (${SITE:-none found}/deploy/)" >&2; exit 1; }
source "$SITE/deploy/$TARGET.env"
OPTS=(-o StrictHostKeyChecking=accept-new -p "${SSH_PORT:-22}")
[[ -n ${SSH_JUMP:-} ]] && OPTS+=(-J "$SSH_JUMP")      # same reachability settings as deploy.sh
if [[ -n ${SSHPASS:-} ]]; then RSH=(sshpass -e ssh "${OPTS[@]}"); else RSH=(ssh "${OPTS[@]}"); fi
KEY=$("${RSH[@]}" "$SSH_USER@$SSH_HOST" '[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519; cat ~/.ssh/id_ed25519.pub')
for bs in "$@"; do
  ssh -o BatchMode=yes "$bs" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && (grep -qF '$KEY' ~/.ssh/authorized_keys || echo '$KEY' >> ~/.ssh/authorized_keys)"
  "${RSH[@]}" "$SSH_USER@$SSH_HOST" "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new $bs hostname" \
    && echo "ok: $TARGET -> $bs"
done
