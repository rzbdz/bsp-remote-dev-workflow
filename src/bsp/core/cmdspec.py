"""Command specifications: one definition drives REST routes, AI tool schemas,
the remote CLI and the web console.

This module is stdlib-only on purpose: the thin `bsp` client imports it to build
its argparse tree from the specs the server publishes at ``GET /api/v1/tools``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import InvalidRequest

PARAM_TYPES = ("string", "integer", "number", "boolean")


@dataclass
class Param:
    name: str
    type: str = "string"
    help: str = ""
    required: bool = True
    default: Any = None
    choices: list[Any] | None = None
    # required params become positionals in the CLI unless positional=False
    positional: bool | None = None

    @property
    def is_positional(self) -> bool:
        return self.required if self.positional is None else self.positional

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"type": self.type}
        if self.help:
            s["description"] = self.help
        if self.choices:
            s["enum"] = list(self.choices)
        if self.default is not None:
            s["default"] = self.default
        return s

    def coerce(self, value: Any) -> Any:
        """Coerce a JSON/query/CLI value to the declared type and validate it."""
        if value is None:
            return None
        try:
            if self.type == "integer":
                if isinstance(value, bool):
                    raise ValueError
                value = int(value)
            elif self.type == "number":
                value = float(value)
            elif self.type == "boolean":
                if isinstance(value, str):
                    low = value.strip().lower()
                    if low in ("1", "true", "yes", "on"):
                        value = True
                    elif low in ("0", "false", "no", "off"):
                        value = False
                    else:
                        raise ValueError
                else:
                    value = bool(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise InvalidRequest(f"parameter '{self.name}' must be {self.type}, got {value!r}") from None
        if self.choices and value not in self.choices:
            raise InvalidRequest(
                f"parameter '{self.name}' must be one of {self.choices}, got {value!r}"
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "help": self.help,
            "required": self.required,
            "default": self.default,
            "choices": self.choices,
            "positional": self.is_positional,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Param":
        return cls(**{k: d.get(k) for k in ("name", "type", "help", "required", "default", "choices", "positional")})


@dataclass
class CommandSpec:
    """Transport-neutral description of an operation.

    ``name`` is dotted (``gpio.set``, ``gpio.alias.add``). The CLI renders it as
    ``bsp gpio set``, the AI tool name is ``gpio_set`` and the REST route is
    ``/api/v1/gpio/{target}/set`` (or ``/api/v1/gpio/alias/add`` without target).
    """

    name: str
    summary: str
    params: list[Param] = field(default_factory=list)
    description: str = ""
    mutating: bool = True

    @property
    def tool_name(self) -> str:
        return self.name.replace(".", "_")

    @property
    def segments(self) -> list[str]:
        return self.name.split(".")

    @property
    def group(self) -> str:
        return self.segments[0]

    @property
    def verb(self) -> str:
        return self.segments[-1]

    @property
    def has_target(self) -> bool:
        return bool(self.params) and self.params[0].name == "target"

    @property
    def http_path(self) -> str:
        segs = self.segments
        if len(segs) == 1:
            return f"/api/v1/{self.name}"
        base = "/".join(segs[:-1])
        if self.has_target:
            return f"/api/v1/{base}/{{target}}/{segs[-1]}"
        return f"/api/v1/{base}/{segs[-1]}"

    @property
    def cli_usage(self) -> str:
        parts = ["bsp", *self.segments]
        for p in self.params:
            if p.is_positional:
                parts.append(f"<{p.name}>")
            else:
                parts.append(f"[--{p.name.replace('_', '-')}]")
        return " ".join(parts)

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.params},
            "required": [p.name for p in self.params if p.required],
            "additionalProperties": False,
        }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        known = {p.name: p for p in self.params}
        unknown = set(args) - set(known)
        if unknown:
            raise InvalidRequest(
                f"{self.name}: unknown parameter(s) {sorted(unknown)}",
                hint=f"usage: {self.cli_usage}",
                details={"accepted": [p.to_dict() for p in self.params]},
            )
        out: dict[str, Any] = {}
        for p in self.params:
            v = args.get(p.name)
            if v is None:
                if p.required:
                    raise InvalidRequest(
                        f"{self.name}: missing required parameter '{p.name}' ({p.help or p.type})",
                        hint=f"usage: {self.cli_usage}",
                        details={"accepted": [q.to_dict() for q in self.params]},
                    )
                v = p.default
            out[p.name] = p.coerce(v)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_name": self.tool_name,
            "summary": self.summary,
            "description": self.description,
            "mutating": self.mutating,
            "params": [p.to_dict() for p in self.params],
            "input_schema": self.input_schema(),
            "http": {"method": "POST" if self.mutating else "GET", "path": self.http_path},
            "cli": self.cli_usage,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CommandSpec":
        return cls(
            name=d["name"],
            summary=d.get("summary", ""),
            description=d.get("description", ""),
            mutating=d.get("mutating", True),
            params=[Param.from_dict(p) for p in d.get("params", [])],
        )


# ---------------------------------------------------------------- argparse --

class CliExit(Exception):
    """Raised by :class:`CapturingParser` instead of calling ``sys.exit``."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message


class CapturingParser(argparse.ArgumentParser):
    """ArgumentParser that never touches stdout/stderr or exits (server-side console)."""

    def _print_message(self, message, file=None):  # help/usage output
        if message:
            raise CliExit(0, message)

    def exit(self, status=0, message=None):
        raise CliExit(status, message or "")

    def error(self, message):
        raise CliExit(2, f"{self.format_usage()}{self.prog}: error: {message}\n")


def build_parser(
    specs: Iterable[CommandSpec],
    prog: str = "bsp",
    parser_class: type[argparse.ArgumentParser] = argparse.ArgumentParser,
    parent: argparse.ArgumentParser | None = None,
) -> argparse.ArgumentParser:
    """Build a nested ``prog seg1 seg2 ... args`` parser. ``_command`` holds the spec name."""
    root = parent or parser_class(prog=prog, description="bsp dev-loop command line")
    groups: dict[tuple[str, ...], argparse.ArgumentParser] = {(): root}
    actions: dict[tuple[str, ...], argparse._SubParsersAction] = {}

    def sub_action(path: tuple[str, ...]) -> argparse._SubParsersAction:
        if path not in actions:
            actions[path] = groups[path].add_subparsers(
                dest=f"_sub{len(path)}", metavar="<command>" if not path else "<action>", parser_class=parser_class
            )
        return actions[path]

    for spec in sorted(specs, key=lambda s: s.name):
        segs = spec.segments
        for i in range(1, len(segs)):
            path = tuple(segs[:i])
            if path not in groups:
                groups[path] = sub_action(path[:-1]).add_parser(segs[i - 1], help=f"{' '.join(path)} commands")
        sp = sub_action(tuple(segs[:-1])).add_parser(
            segs[-1], help=spec.summary, description=spec.description or spec.summary
        )
        sp.set_defaults(_command=spec.name)
        for p in spec.params:
            kw: dict[str, Any] = {"help": p.help or None}
            if p.choices:
                kw["choices"] = p.choices
            if p.type == "integer":
                kw["type"] = int
            elif p.type == "number":
                kw["type"] = float
            if p.is_positional:
                if not p.required:
                    kw["nargs"] = "?"
                sp.add_argument(p.name, **kw)
            else:
                flag = "--" + p.name.replace("_", "-")
                if p.type == "boolean":
                    kw.pop("type", None)
                    if hasattr(argparse, "BooleanOptionalAction"):
                        sp.add_argument(flag, dest=p.name, action=argparse.BooleanOptionalAction,
                                        default=None, help=p.help)
                    else:  # Python 3.8 (build servers)
                        sp.add_argument(flag, dest=p.name, action="store_true", default=None, help=p.help)
                        sp.add_argument("--no-" + flag[2:], dest=p.name, action="store_false", default=None)
                else:
                    if p.default is not None:
                        kw["help"] = f"{p.help} (default: {p.default})"
                    sp.add_argument(flag, dest=p.name, default=None, required=p.required, **kw)
    return root


def parse_tokens(parser: argparse.ArgumentParser, tokens: list[str]) -> tuple[str, dict[str, Any]]:
    """Return ``(command_name, args)``; args only contain explicitly given values."""
    ns = parser.parse_args(tokens)
    name = getattr(ns, "_command", None)
    if not name:
        # group(s) given without a leaf command (or nothing at all): show that level's help
        parser.parse_args(tokens + ["--help"])
    args = {k: v for k, v in vars(ns).items() if not k.startswith("_") and v is not None}
    return name, args
