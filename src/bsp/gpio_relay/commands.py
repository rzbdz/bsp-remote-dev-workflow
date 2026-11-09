"""gpio_relay command set: the single definition behind REST, CLI, console and AI tools."""

from __future__ import annotations

from typing import Any

from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command
from .service import GpioRelayService

TARGET = Param("target", help="pin ref: 33 | P33 | J40:33 | GPIO127 | alias (e.g. DUT3/reset)")
FORCE = Param("force", "boolean", "write even if the pad is muxed to another function", required=False, default=False)
WAIT = Param("wait", "boolean", "block until the pulse finishes", required=False, default=True)


# ------------------------------------------------------------------ render --
def _note(p: dict[str, Any]) -> str:
    notes = []
    if p.get("alt"):
        notes.append(p["alt"])
    mux = p.get("pinmux") or {}
    if mux.get("conflict"):
        notes.append(f"MUX:{mux.get('function') or mux.get('mux_owner')}")
    if p.get("protected"):
        notes.append("PROTECTED")
    if p.get("busy"):
        notes.append(f"BUSY:{p['busy']}")
    if p.get("error"):
        notes.append(f"ERR:{p['error']['code']}")
    return " ".join(notes)


def render_pin(p: dict[str, Any]) -> str:
    if p.get("kind") != "gpio":
        return f"{p['target']:<5} {p['label']}"
    ch = p.get("channel") or {}
    line = p.get("line") or {}
    lv = "-" if not line or not line.get("claimed") else f"{line.get('direction')}/{line.get('value')}"
    opts = ""
    if ch.get("mode") != "gpio":
        opts = f" active_low={ch.get('active_low')} drive={ch.get('drive')}"
    return (
        f"{p['target']:<5} {p['label']:<8} mode={ch.get('mode'):<6} aliases={','.join(p.get('aliases') or []) or '-'}"
        f" state={p.get('state')} line={lv}{opts} {_note(p)}".rstrip()
    )


def render_table(pins: list[dict[str, Any]]) -> str:
    rows = [("PIN", "SIGNAL", "MODE", "ALIASES", "STATE", "LINE", "NOTE")]
    for p in pins:
        if p.get("kind") != "gpio":
            rows.append((p["target"], p["label"], "", "", "", "", ""))
            continue
        ch = p.get("channel") or {}
        line = p.get("line") or {}
        lv = "-" if not line or not line.get("claimed") else f"{line.get('direction')}/{line.get('value')}"
        rows.append((p["target"], p["label"], ch.get("mode", ""), ",".join(p.get("aliases") or []) or "-",
                     str(p.get("state")), lv, _note(p)))
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def render_board(b: dict[str, Any]) -> str:
    be = b["backend"]
    pm = b["pinmux"]
    lines = [
        f"board   : {b['name']} ({b['id']})",
        f"headers : " + ", ".join(f"{h['id']} {h['title']} [{h['layout']}]" for h in b["headers"]),
        f"backend : {be['name']}  degraded={be.get('degraded')}  timeout={be.get('timeout_s')}s",
        f"pinmux  : {pm['state']}" + (f" ({pm['error']})" if pm.get("error") else ""),
        "modes   : " + "; ".join(f"{m}: {', '.join(v['actions'])}" for m, v in b["modes"].items()),
    ]
    return "\n".join(lines)


def render_aliases(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(no aliases)"
    rows = [("ALIAS", "PIN", "GPIO", "MODE", "ORIGIN", "TAGS", "OWNER", "NOTE")]
    for a in items:
        rows.append((a["name"], a.get("pin", ""), a.get("label", ""), a.get("mode", ""), a.get("origin", ""),
                     ",".join(a.get("tags") or []), a.get("owner") or "", a.get("note") or ""))
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def render_yaml(d: Any) -> str:
    import yaml

    return yaml.safe_dump(d, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------- commands --
def build_commands(svc: GpioRelayService) -> list[Command]:
    def cmd(name, summary, handler, params=(), render=render_pin, mutating=True, description=""):
        return Command(CommandSpec(name, summary, list(params), description, mutating), handler, render)

    return [
        cmd("board.info", "Board profile, header layout, backend and pinmux status",
            svc.board_info, render=render_board, mutating=False),
        cmd("gpio.list", "List header pins with live line state",
            lambda all: svc.list_pins(gpio_only=not all),
            [Param("all", "boolean", "include power/GND pins", required=False, default=False)],
            render=render_table, mutating=False),
        cmd("gpio.get", "Read one pin: mode, logical state, physical line", svc.get, [TARGET], mutating=False),
        cmd("gpio.set", "Drive a gpio-mode pin high / low, or switch it to input (hi-Z)",
            lambda target, level, force: svc.set_level(target, level, force),
            [TARGET, Param("level", help="high | low | input", choices=["high", "low", "input"]), FORCE]),
        cmd("gpio.pulse", "Pulse a gpio-mode pin to LEVEL for MS milliseconds, then restore the previous state",
            lambda target, level, ms, wait, force: svc.pulse(target, ms, level, wait, force),
            [TARGET, Param("level", help="pulse level", required=False, default="low", choices=["high", "low"]),
             Param("ms", "integer", "pulse width in ms", required=False, default=100), WAIT, FORCE]),
        cmd("gpio.free", "Release the line (input + unexport); hardware returns to its default", svc.free, [TARGET]),
        cmd("gpio.config", "Change a pin's channel mode / polarity / drive / initial state (persisted as runtime overlay)",
            lambda target, reset, **chg: svc.configure(target, chg, reset),
            [TARGET,
             Param("mode", help="channel mode", required=False, choices=["gpio", "relay", "button"]),
             Param("active_low", "boolean", "relay/button: active level is low", required=False),
             Param("drive", help="relay/button inactive drive", required=False, choices=["push_pull", "open_drain", ""]),
             Param("initial", help="state applied at service start (gpio: high|low|input, relay/button: on|off, '' clears)", required=False),
             Param("description", help="free text", required=False),
             Param("reset", "boolean", "drop runtime overlay, back to host config", required=False, default=False)],
            description="Mode semantics: gpio = raw level; relay = on/off with polarity (active_low) and drive "
                        "(push_pull drives the inactive level, open_drain floats it); button = relay preset with "
                        "active_low + open_drain: press pulls the net to GND, release leaves it hi-Z."),
        cmd("gpio.export", "Export non-default channel configs and runtime aliases as host-config YAML", svc.export_config,
            render=render_yaml, mutating=False),
        cmd("gpio.pinmux", "Re-read pad mux ownership from debugfs (flags pins owned by other functions)",
            svc.refresh_pinmux, render=render_yaml, mutating=False),
        # ---- aliases: downstream layers (DUT manager, ...) register meaning, this layer only resolves
        cmd("gpio.alias.list", "List aliases (filter by path prefix such as DUT3, by tag, or by target pin)",
            svc.alias_list,
            [Param("prefix", help="alias path prefix, e.g. DUT3", required=False, positional=True),
             Param("tag", help="only aliases carrying this tag", required=False),
             Param("target", help="only aliases of this pin", required=False, positional=False)],
            render=render_aliases, mutating=False),
        cmd("gpio.alias.add", "Bind an alias (e.g. DUT3/reset) to a GPIO pin; usable as target everywhere",
            svc.alias_add,
            [Param("name", help="alias path, e.g. DUT3/reset"),
             Param("target", help="pin ref or existing alias", positional=True),
             Param("tags", help="comma separated tags, e.g. dut3,reset", required=False),
             Param("note", help="free text", required=False),
             Param("owner", help="registering component, e.g. dut-manager", required=False),
             Param("replace", "boolean", "re-point an existing runtime alias", required=False, default=False)],
            render=lambda a: f"alias {a['name']} -> {a['pin']} ({a['label']}, {a['mode']} mode)",
            description="Idempotent: re-adding the same name->pin succeeds. Different pin needs replace=true. "
                        "Aliases from the host config are read-only."),
        cmd("gpio.alias.delete", "Remove an alias, or a whole alias subtree with --prefix (e.g. DUT3)",
            svc.alias_delete,
            [Param("name", help="alias (or prefix with --prefix)"),
             Param("prefix", "boolean", "delete every alias under this path prefix", required=False, default=False)],
            render=lambda gone: "deleted: " + ", ".join(a["name"] for a in gone)),
        cmd("gpio.alias.resolve", "Show what an alias or pin ref resolves to (with live pin state)",
            svc.alias_resolve, [Param("name", help="alias or pin ref")],
            render=lambda r: (f"{r['input']} -> " if not r["native"] else "") + render_pin(r["pin"]),
            mutating=False),
        cmd("relay.on", "Switch a relay/button channel on (active level)",
            lambda target, force: svc.switch(target, "on", force), [TARGET, FORCE]),
        cmd("relay.off", "Switch a relay/button channel off",
            lambda target, force: svc.switch(target, "off", force), [TARGET, FORCE]),
        cmd("relay.toggle", "Toggle a relay/button channel",
            lambda target, force: svc.switch(target, "toggle", force), [TARGET, FORCE]),
        cmd("relay.pulse", "Switch on for MS milliseconds, then off",
            lambda target, ms, wait, force: svc.pulse(target, ms, None, wait, force),
            [TARGET, Param("ms", "integer", "on time in ms", required=False, default=200), WAIT, FORCE]),
        cmd("button.press", "Press and hold (pull the button net to its active level)",
            lambda target, force: svc.switch(target, "press", force), [TARGET, FORCE]),
        cmd("button.release", "Release (hi-Z for open-drain buttons)",
            lambda target, force: svc.switch(target, "release", force), [TARGET, FORCE]),
        cmd("button.click", "Press for MS milliseconds, then release (e.g. tap DUT reset)",
            lambda target, ms, wait, force: svc.pulse(target, ms, None, wait, force),
            [TARGET, Param("ms", "integer", "press time in ms", required=False, default=200), WAIT, FORCE]),
    ]
