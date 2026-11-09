#!/usr/bin/env bash
# Install (or update) the bsp client on a workstation / build server.
#
#   deploy/install-client.sh <user@host> [DIR]       default DIR: ~/bsp-dev-loop
#   deploy/install-client.sh local [DIR]
#   ... --site <site-dir>    also ship a site directory to ~/.config/bsp/site
#                            (minus .git and the patterns in <site>/.bspignore)
#
# Ships the git-tracked tree (clean HEAD unless --allow-dirty) and links
# ~/.local/bin/bsp -> DIR/bin/bsp. Needs python3 >= 3.8 (+ PyYAML for `bsp sdk`).
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET=${1:?usage: deploy/install-client.sh <user@host|local> [DIR] [--allow-dirty]}
DIR=${2:-'~/bsp-dev-loop'}
[[ $DIR == --* ]] && DIR='~/bsp-dev-loop'
SITE=""
args=("$@"); for i in "${!args[@]}"; do [[ ${args[$i]} == --site ]] && SITE=${args[$((i+1))]}; done
REV=$(git rev-parse --short HEAD)
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  [[ " $* " == *" --allow-dirty "* ]] || { echo "uncommitted changes; commit or pass --allow-dirty" >&2; exit 1; }
  REV="$REV-dirty"
fi
INSTALL="set -e; D=$DIR; mkdir -p \$D; tar -xzf - -C \$D; echo $REV > \$D/REVISION;
  mkdir -p ~/.local/bin; ln -sf \$D/bin/bsp ~/.local/bin/bsp;
  python3 -c 'import yaml' 2>/dev/null || echo 'note: PyYAML missing: bsp sdk needs it (pip install --user pyyaml)';
  echo \"bsp client $REV installed in \$D (~/.local/bin/bsp)\""
if [[ $TARGET == local ]]; then
  git ls-files -z | tar --null -czf - -T - | bash -c "$INSTALL"
else
  git ls-files -z | tar --null -czf - -T - | ssh -o BatchMode=yes "$TARGET" "$INSTALL"
fi
if [[ -n $SITE ]]; then
  # never replace a site that is a symlink or a git checkout: that would drop its history
  SITE_INSTALL='set -e; T=~/.config/bsp/site
    if [ -L "$T" ] || [ -d "$T/.git" ]; then echo "refusing to replace $T: it is a symlink or git checkout (update it with git, or point BSP_SITE at it)" >&2; exit 1; fi
    mkdir -p ~/.config/bsp; rm -rf ~/.config/bsp/site.new; mkdir ~/.config/bsp/site.new;
    tar -xzf - -C ~/.config/bsp/site.new; rm -rf ~/.config/bsp/site; mv ~/.config/bsp/site.new ~/.config/bsp/site;
    echo "site installed in ~/.config/bsp/site"'
  EXCL=(--exclude=.git); [[ -f $SITE/.bspignore ]] && EXCL+=(--exclude-from="$SITE/.bspignore")
  if [[ $TARGET == local ]]; then tar -C "$SITE" "${EXCL[@]}" -czf - . | bash -c "$SITE_INSTALL"
  else tar -C "$SITE" "${EXCL[@]}" -czf - . | ssh -o BatchMode=yes "$TARGET" "$SITE_INSTALL"; fi
fi
