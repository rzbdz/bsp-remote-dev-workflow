"""Executable commands: a spec bound to a handler, plus a registry (command pattern)."""

from __future__ import annotations

import difflib
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from .cmdspec import CapturingParser, CliExit, CommandSpec, build_parser, parse_tokens
from .errors import NotFound, BspError


@dataclass
class Command:
    spec: CommandSpec
    handler: Callable[..., Any]
    # Optional human renderer used by the CLI / web console; JSON is always available.
    render: Callable[[Any], str] | None = None

    def run(self, args: dict[str, Any]) -> Any:
        return self.handler(**self.spec.validate(args))


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._by_tool: dict[str, Command] = {}

    def add(self, cmd: Command) -> None:
        if cmd.spec.name in self._commands or cmd.spec.tool_name in self._by_tool:
            raise ValueError(f"duplicate command {cmd.spec.name}")
        self._commands[cmd.spec.name] = cmd
        self._by_tool[cmd.spec.tool_name] = cmd

    def extend(self, cmds) -> None:
        for c in cmds:
            self.add(c)

    def get(self, name: str) -> Command:
        cmd = self._commands.get(name) or self._by_tool.get(name)
        if cmd:
            return cmd
        names = list(self._commands) + list(self._by_tool)
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.5)
        raise NotFound(
            f"unknown command '{name}'",
            hint=f"did you mean: {', '.join(close)}?" if close else "GET /api/v1/tools (or `bsp help`) lists all commands",
            details={"did_you_mean": close},
        )

    def specs(self) -> list[CommandSpec]:
        return [c.spec for c in sorted(self._commands.values(), key=lambda c: c.spec.name)]

    def execute(self, name: str, args: dict[str, Any]) -> tuple[Any, str | None]:
        cmd = self.get(name)
        result = cmd.run(args)
        text = cmd.render(result) if cmd.render else None
        return result, text

    def run_line(self, line: str) -> tuple[int, str, Any]:
        """Execute one CLI line (web console). Returns ``(exit_code, output, result)``."""
        import json

        try:
            tokens = shlex.split(line)
        except ValueError as e:
            return 2, f"parse error: {e}\n", None
        if tokens and tokens[0] == "bsp":
            tokens = tokens[1:]
        if not tokens:
            tokens = ["--help"]
        elif tokens[0] in ("help", "?"):  # `help gpio set` == `gpio set --help`
            tokens = tokens[1:] + ["--help"]
        parser = build_parser(self.specs(), prog="bsp", parser_class=CapturingParser)
        try:
            name, args = parse_tokens(parser, tokens)
        except CliExit as e:
            return e.status, e.message, None
        try:
            result, text = self.execute(name, args)
        except BspError as e:
            msg = f"error[{e.code}]: {e.message}\n"
            if e.hint:
                msg += f"hint: {e.hint}\n"
            return 1, msg, None
        if text is None:
            text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return 0, text if text.endswith("\n") else text + "\n", result
