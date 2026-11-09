#!/bin/bash
# Register router port mappings for bsp services via UPnP IGD (needs `upnpc` from
# miniupnpc). Ports from /etc/bsp/upnp.env: UPNP_PORTS, optional UPNP_ROUTE_PROBE (an
# address used only to find the outgoing interface; default 192.0.2.1, never contacted).
set -u
# shellcheck disable=SC1091
source /etc/bsp/upnp.env
LOG=/var/log/bsp-upnp.log
LOCAL_IP=$(ip route get "${UPNP_ROUTE_PROBE:-192.0.2.1}" 2>/dev/null | grep -oP 'src \K\S+')
if [ -z "$LOCAL_IP" ]; then
  echo "[$(date)] ERROR: could not determine local IP" >> "$LOG"
  exit 1
fi
rc=0
for p in $UPNP_PORTS; do
  upnpc -d "$p" TCP >/dev/null 2>&1 || true
  if upnpc -e "bsp-hostd-$p" -a "$LOCAL_IP" "$p" "$p" TCP 0 >> "$LOG" 2>&1; then
    echo "[$(date)] registered $LOCAL_IP:$p -> external :$p" >> "$LOG"
  else
    echo "[$(date)] ERROR: mapping $p failed" >> "$LOG"
    rc=1
  fi
done
exit $rc
