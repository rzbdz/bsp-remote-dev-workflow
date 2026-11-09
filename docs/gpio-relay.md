# GPIO relay

The `gpio_relay` module turns a board host's GPIO header into named, typed channels: raw GPIO
lines, relays and push buttons. The `dut` module uses it to press the reset and download-mode
buttons of the boards; you can use it directly for fans, power relays or anything else on the
header.

## Addressing a pin

Every command takes a *target*, written as any of:

| Form | Example | Meaning |
|---|---|---|
| Physical pin | `33`, `P33`, `J40:33` | Pin number on the header (connector name optional) |
| Linux GPIO | `GPIO127`, `gpio:127` | Kernel GPIO number |
| Alias | `board-1/reset` | A name registered for a pin |

The header layout — pin numbers, power and ground pins, GPIO numbers, alternate functions — comes
from a board map (`boards/<id>.yaml`) in a pack or the site, selected by
`modules.gpio_relay.board`.

## Channel modes

| Mode | Actions | Behaviour |
|---|---|---|
| `gpio` | `high`, `low`, `input`, `pulse`, `free` | Raw level control |
| `relay` | `on`, `off`, `toggle`, `pulse` | On/off semantics with configurable `active_low` and `drive` (`push_pull` or `open_drain`) |
| `button` | `press`, `release`, `click` | A relay preset: active low, open drain. Pressing pulls the line to ground; releasing leaves it floating |

```bash
bsp -r lab-1 gpio list
bsp -r lab-1 gpio config P11 --mode button
bsp -r lab-1 button click P11 --ms 300
bsp -r lab-1 relay on bench/fan
```

Changes made with `gpio config` (or in the web console) are stored on the host as a runtime
overlay. `bsp gpio export` prints the merged channels and aliases as YAML for
`hosts/<host>.yaml`, where they become the baseline.

## Wiring a reset button

Most development boards have reset and boot buttons that short a signal to ground. To press them
from the host:

- connect the host's ground to the board's ground;
- connect a host GPIO to the button's signal side (the pad that is not ground);
- configure that pin as a `button` channel.

In `button` mode the host only ever pulls the line low or releases it — it never drives it high —
so a 3.3 V host can safely operate a button on a board whose logic runs at a lower voltage, as
long as the board pulls the line up itself. Check the board's schematic, and use a relay or an
optocoupler when grounds cannot be shared or the line is not a simple pull-up input.

List pins that must never be driven (pins used by the host itself, or wired to something
sensitive) under `protected:` in the host configuration.

## Aliases

Aliases give pins meaningful, hierarchical names such as `board-1/reset`. Components register
the aliases they own — the `dut` module registers `<dut>/reset` and `<dut>/fdl` for each board —
so a command can be passed straight through: `bsp dut reset board-1` becomes
`button click board-1/reset` on the right pin.

- Names are case-insensitive paths; they cannot look like a pin reference (`P12`, `33`, `GPIO5`).
- Registering the same mapping twice is a no-op; pointing a name elsewhere needs `--replace`.
- `bsp gpio alias list board-1` filters by prefix; `bsp gpio alias delete board-1 --prefix`
  removes a whole subtree.
- Aliases in the host configuration are read-only.

## Pin conflicts and hardware safety

When `pinmux: true`, the host reads the kernel's pin-mux ownership and marks pins whose pad is
assigned to another function (I²C, UART, …). Writes to such pins are refused unless you pass
`--force`, because they would not reach the pin. Hardware calls run with a timeout; if the kernel
blocks, the backend is marked degraded and the web console shows a banner instead of hanging.

## HTTP

```bash
curl http://HOST:8765/api/v1/gpio/list
curl -X POST http://HOST:8765/api/v1/button/board-1%2Freset/click \
     -H 'Authorization: Bearer TOKEN' -H 'Content-Type: application/json' -d '{"ms": 300}'
```

Read requests use `GET`; changes use `POST` and need the token when the host has one. The web
console at `http://HOST:8765/` shows the header map, a pin inspector, the event stream, an API
reference and a remote CLI.
