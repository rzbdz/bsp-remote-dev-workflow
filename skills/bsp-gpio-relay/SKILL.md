---
name: bsp-gpio-relay
description: Control DUT reset / FDL (download-mode) lines and relays through the bsp host controller's GPIO header. Use when you need to reset a board, hold it in FDL/fastboot boot mode, toggle a relay, or inspect header pin state on a bsp board host (see `bsp hosts`).
---

# bsp gpio-relay

A bsp host controller exposes its GPIO header as channels. Each channel is a
header pin in one of three modes:

- `gpio`   raw level: `high` / `low` / `input` (hi-Z)
- `relay`  `on` / `off` / `toggle` / `pulse`, with `active_low` and `drive` options
- `button` momentary switch on a DUT button net with shared GND:
           `press` = pull to GND, `release` = hi-Z, `click` = press for N ms

Pin targets: `33`, `P33`, `J40:33` (physical pin), `GPIO127` (Linux number), or an
alias such as `DUT3/reset`. Aliases are registered by downstream components; this
service only resolves them.

## Discover first

```bash
curl -s $BSP_URL/llms.txt                         # overview + every tool
curl -s $BSP_URL/api/v1/tools?format=anthropic    # JSON-schema tool definitions
curl -s $BSP_URL/api/v1/gpio/list                 # live state of every header pin
```

Or with the CLI (stdlib only, from the repo): `BSP_URL=http://<host>:8765 bin/bsp gpio list`.

## Common recipes

| Goal | CLI | HTTP |
|---|---|---|
| Register meaning | `bsp gpio alias add DUT3/reset P11 --owner <you>` | `POST /api/v1/gpio/alias/add {"name":"DUT3/reset","target":"P11","owner":"<you>"}` |
| Reset DUT | `bsp button click DUT3/reset --ms 300` | `POST /api/v1/button/DUT3/reset/click {"ms":300}` |
| Boot into FDL | `bsp button press DUT3/fdl` → `bsp button click DUT3/reset` → wait for USB download device → `bsp button release DUT3/fdl` | same verbs under `/api/v1/button/...` |
| Relay on/off | `bsp relay on DUT3/power` / `off` | `POST /api/v1/relay/DUT3/power/on` |
| Raw level | `bsp gpio set P7 low` | `POST /api/v1/gpio/P7/set?level=low` |
| Re-mode a pin | `bsp gpio config P11 --mode button` | `POST /api/v1/gpio/P11/config {"mode":"button"}` |
| Inspect a DUT's lines | `bsp gpio alias list DUT3` | `GET /api/v1/gpio/alias/list?prefix=DUT3` |
| Unregister a DUT | `bsp gpio alias delete DUT3 --prefix` | `POST /api/v1/gpio/alias/delete {"name":"DUT3","prefix":true}` |
| Watch events | `bsp watch` | `GET /api/v1/events` (SSE) |

Generic call for any tool: `POST /api/v1/tools/<tool_name>` with the JSON args.

## Rules

- Check `gpio list` / `gpio alias list` before acting; aliases are host-specific. Do not guess which pin is wired to what.
- Errors are diagnostic: read `error.hint` first (did-you-mean, valid actions for the current mode, the fixing command), then `error.details`.
- Prefer `button` mode for lines wired directly to DUT buttons: release is hi-Z, so the host never drives 3.3 V into the DUT net.
- `gpio set` only works in gpio mode (by design). Do not switch an aliased button/relay channel to gpio mode unless asked.
- `409 conflict` with pinmux details: the pad belongs to another function (e.g. I2C). Do not pass `force=true` unless the user explicitly asks.
- `403 forbidden`: the pin is protected in the host config. Do not work around it.
- `504 hardware_timeout` / backend `degraded`: kernel GPIO calls are blocked. Stop and report; the host likely needs a reboot.
- Every action is logged as an event; mention the event/time when reporting what you did.
