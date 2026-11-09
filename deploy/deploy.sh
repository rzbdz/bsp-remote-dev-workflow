#!/usr/bin/env bash
# Reproducible deploy of bsp-hostd to a board host.
#
#   SSHPASS=<password> deploy/deploy.sh <target> [deploy|status|logs|restart|ssh] [--allow-dirty]
#
# Site data comes from the site directory ($BSP_SITE, ~/.config/bsp/site, /etc/bsp/site):
# <site>/deploy/<target>.env says how to reach the host, <site>/hosts/<target>.yaml is its
# config. The whole site directory is shipped to <INSTALL_DIR>/site next to the code. `deploy` ships the git-tracked tree
# (clean HEAD unless --allow-dirty), installs it into a venv on the host, renders
# the systemd unit, registers UPnP port mappings and health-checks the service.
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET=${1:?usage: deploy/deploy.sh <target> [deploy|status|logs|restart|ssh] [--allow-dirty]}
ACTION=${2:-deploy}
ALLOW_DIRTY=0
for a in "$@"; do [[ $a == --allow-dirty ]] && ALLOW_DIRTY=1; done
[[ $ACTION == --allow-dirty ]] && ACTION=deploy

SITE=${BSP_SITE:-}
for c in "$SITE" "$HOME/.config/bsp/site" /etc/bsp/site; do [[ -n $c && -d $c ]] && { SITE=$(cd "$c" && pwd); break; }; done
[[ -n $SITE && -d $SITE ]] || { echo "no site directory (set BSP_SITE; see examples/site)" >&2; exit 1; }
ENV_FILE=$SITE/deploy/$TARGET.env
[[ -f $ENV_FILE ]] || { echo "no such target: $ENV_FILE (targets: $(ls "$SITE/deploy" 2>/dev/null | sed 's/\.env$//' | tr '\n' ' '))" >&2; exit 1; }
[[ -f $SITE/hosts/$TARGET.yaml ]] || { echo "no host config $SITE/hosts/$TARGET.yaml" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
# SSH_JUMP=user@host (env or target file): reach the target through a jump host when
# this machine has no direct route. A local port-forward tunnel keeps one password
# prompt per ssh login, so sshpass works for both hops.
if [[ -n ${SSH_JUMP:-} ]]; then
  JUMP_PORT=$((20000 + RANDOM % 20000))
  if [[ -n ${SSHPASS:-} ]]; then JSSH=(sshpass -e ssh); else JSSH=(ssh); fi
  "${JSSH[@]}" "${SSH_OPTS[@]}" -o ExitOnForwardFailure=yes -f -N \
    -L "127.0.0.1:$JUMP_PORT:$SSH_HOST:${SSH_PORT:-22}" "$SSH_JUMP"
  trap 'pkill -f "127.0.0.1:$JUMP_PORT:$SSH_HOST" || true' EXIT
  echo "==> via jump host $SSH_JUMP (tunnel 127.0.0.1:$JUMP_PORT -> $SSH_HOST:${SSH_PORT:-22})"
  SSH_HOST=127.0.0.1
  SSH_PORT=$JUMP_PORT
fi
SSH_OPTS+=(-p "${SSH_PORT:-22}")
if [[ -n ${SSHPASS:-} ]]; then SSH=(sshpass -e ssh "${SSH_OPTS[@]}"); else SSH=(ssh "${SSH_OPTS[@]}"); fi
REMOTE="$SSH_USER@$SSH_HOST"
rsh() { "${SSH[@]}" "$REMOTE" "$@"; }

case $ACTION in
  status)  rsh "systemctl --no-pager status bsp-hostd | head -15; curl -fsS localhost:$SERVICE_PORT/api/v1/status; echo"; exit ;;
  logs)    rsh "journalctl -u bsp-hostd --no-pager -n ${LINES:-100}"; exit ;;
  restart) rsh "systemctl restart bsp-hostd && sleep 2 && systemctl is-active bsp-hostd"; exit ;;
  ssh)     "${SSH[@]}" -t "$REMOTE"; exit ;;
  deploy)  ;;
  *) echo "unknown action $ACTION" >&2; exit 1 ;;
esac

REV=$(git rev-parse --short HEAD)
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  if [[ $ALLOW_DIRTY == 1 ]]; then REV="$REV-dirty"; else
    echo "working tree has uncommitted changes; commit first or pass --allow-dirty" >&2; exit 1
  fi
fi

echo "==> shipping $REV to $REMOTE:$INSTALL_DIR"
# --allow-dirty also ships new files that are not committed yet (but never ignored ones)
{ git ls-files -z --recurse-submodules
  if [[ $ALLOW_DIRTY == 1 ]]; then git ls-files -z --others --exclude-standard; fi; } \
  | tar --null -czf - -T - \
  | rsh "set -e; mkdir -p '$INSTALL_DIR'; rm -rf '$INSTALL_DIR/app.new'; mkdir '$INSTALL_DIR/app.new'; tar -xzf - -C '$INSTALL_DIR/app.new'; echo '$REV' > '$INSTALL_DIR/app.new/REVISION'"

# Images the site lists but does not keep (media-boot helpers, probe images) are fetched here, once, into
# <site>/.blobs/<sha256>/ and shipped with the site: hosts never fetch anything themselves.
echo "==> fetching site images"
BSP_SITE="$SITE" "$(pwd)/bin/bsp" images sync || echo "warning: some site images could not be fetched (see above)" >&2

echo "==> shipping site $SITE"
EXCL=(--exclude=.git); [[ -f $SITE/.bspignore ]] && EXCL+=(--exclude-from="$SITE/.bspignore")   # site-owned excludes
tar -C "$SITE" "${EXCL[@]}" -czf - . \
  | rsh "set -e; rm -rf '$INSTALL_DIR/site.new'; mkdir -p '$INSTALL_DIR/site.new'; tar -xzf - -C '$INSTALL_DIR/site.new'"

echo "==> installing"
rsh "INSTALL_DIR='$INSTALL_DIR' HOST_ID='$TARGET' SERVICE_PORT='$SERVICE_PORT' UPNP_PORTS='${UPNP_PORTS:-}' bash '$INSTALL_DIR/app.new/deploy/remote-install.sh'"

# HEALTH_URL: how *this* machine reaches the service (defaults to EXTERNAL_URL, the user-facing URL)
CHECK_URL=${HEALTH_URL:-${EXTERNAL_URL:-}}
if [[ -n $CHECK_URL ]]; then
  echo "==> checking $CHECK_URL from here"
  for _ in 1 2 3 4 5; do
    if curl -fsS -m 5 "$CHECK_URL/api/v1/health"; then echo; echo "==> OK: open ${EXTERNAL_URL:-$CHECK_URL}"; exit 0; fi
    sleep 2
  done
  echo "!! service is up on the host but $CHECK_URL is not reachable from here (router/UPnP/firewall?)" >&2
  exit 2
fi
