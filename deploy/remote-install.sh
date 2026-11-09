#!/usr/bin/env bash
# Runs ON the host controller (invoked by deploy.sh). Idempotent.
# env: INSTALL_DIR HOST_ID SERVICE_PORT UPNP_PORTS   (code in app.new/, site in site.new/)
set -euo pipefail
: "${INSTALL_DIR:?}" "${HOST_ID:?}" "${SERVICE_PORT:?}"
cd "$INSTALL_DIR"

[[ -d app.new ]] || { echo "app.new missing" >&2; exit 1; }
rm -rf app.old
[[ -d app ]] && mv app app.old
mv app.new app
rm -rf site.old; [[ -d site ]] && mv site site.old
mv site.new site
[[ -f "site/hosts/$HOST_ID.yaml" ]] || { echo "host config site/hosts/$HOST_ID.yaml missing" >&2; exit 1; }
find site/hooks -type f -exec chmod +x {} + 2>/dev/null || true

if [[ ! -x venv/bin/python ]]; then
  echo "--> creating venv"
  python3 -m venv venv
fi
# distro pips can be too old to recognise newer manylinux wheels (e.g. riscv64) and fall
# back to building pydantic-core from Rust sources, so upgrade first
venv/bin/pip install -q --disable-pip-version-check -U "pip>=25"
echo "--> pip install (deps cached after first run)"
# editable: hooks/ and skills/ are found next to the source tree
EXTRAS=server
# the gpiod backend needs the gpiod Python package (and the libgpiod system library)
grep -Eq '^\s*backend:\s*gpiod' "site/hosts/$HOST_ID.yaml" && EXTRAS=server,gpiod
venv/bin/pip install -q --disable-pip-version-check -e "$INSTALL_DIR/app[$EXTRAS]"
if [[ -f app/third_party/serial-mux/pyproject.toml ]]; then
  echo "--> serial-mux (submodule) into the bsp venv"
  venv/bin/pip install -q --disable-pip-version-check -e app/third_party/serial-mux
elif ! command -v serial-mux >/dev/null; then
  echo "WARNING: third_party/serial-mux is empty and serial-mux is not installed on this host:" >&2
  echo "         serial consoles will not work. Clone bsp with --recurse-submodules and redeploy." >&2
fi
ln -sf "$INSTALL_DIR/venv/bin/bsp" /usr/local/bin/bsp
# smtty/serial-mux on PATH only if the host has none of its own (a host-wide serial-mux install takes precedence)
# (a dangling or bsp-made link in /usr/local/bin is replaced)
for b in smtty serial-mux; do
  if [[ -L /usr/local/bin/$b ]] || ! command -v $b >/dev/null; then ln -sfn "$INSTALL_DIR/venv/bin/$b" /usr/local/bin/$b; fi
done

echo "--> systemd unit"
sed -e "s#@INSTALL_DIR@#$INSTALL_DIR#g" -e "s#@HOST_ID@#$HOST_ID#g" \
  app/deploy/systemd/bsp-hostd.service > /etc/systemd/system/bsp-hostd.service

mkdir -p /etc/bsp
if [[ -n ${UPNP_PORTS:-} ]]; then
  echo "--> UPnP port mappings: $UPNP_PORTS"
  printf 'UPNP_PORTS="%s"\n' "$UPNP_PORTS" > /etc/bsp/upnp.env
  install -m 755 app/deploy/upnp/bsp-upnp-register.sh /usr/local/bin/bsp-upnp-register.sh
  cp app/deploy/upnp/bsp-upnp.service app/deploy/upnp/bsp-upnp.timer /etc/systemd/system/
fi

systemctl daemon-reload
systemctl enable -q bsp-hostd.service
systemctl restart bsp-hostd.service
if [[ -n ${UPNP_PORTS:-} ]]; then
  systemctl enable -q --now bsp-upnp.timer
  systemctl start bsp-upnp.service || echo "!! UPnP registration failed (see /var/log/bsp-upnp.log)"
fi

echo "--> health check"
for _ in $(seq 1 20); do
  if curl -fsS -m 2 "http://127.0.0.1:$SERVICE_PORT/api/v1/health" >/dev/null 2>&1; then
    echo "bsp-hostd up ($(cat app/REVISION)) on :$SERVICE_PORT"
    exit 0
  fi
  sleep 1
done
echo "!! bsp-hostd did not become healthy" >&2
journalctl -u bsp-hostd --no-pager -n 40 >&2
exit 1
