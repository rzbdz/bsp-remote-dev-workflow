"""Hooks: replaceable executables with a JSON contract.

A hook is any executable file ``<hooks-dir>/<kind>/<name>`` (any language).

    stdin   one JSON object: the request (documented per kind in hooks/README.md)
    stdout  progress lines (free text) and, as the LAST line, one JSON object: the result
    exit    0 ok | 2 not applicable / nothing found (result still parsed) | other = failure
            (stderr / last stdout lines are surfaced in the error)

Search order (first match wins): $BSP_HOOKS_PATH (":"-separated), the caller's dirs
(host config, then layers: site before packs), ~/.config/bsp/hooks, then the core
hooks/ of the repo — so a site or a pack can add or override a hook without
touching core.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import HardwareError, HardwareTimeout, InvalidRequest, NotFound

REPO_HOOKS = Path(__file__).resolve().parents[3] / "hooks"


@dataclass
class HookResult:
    name: str
    code: int
    data: dict[str, Any]
    lines: list[str]
    elapsed_s: float

    @property
    def applicable(self) -> bool:
        return self.code == 0


class HookRunner:
    def __init__(self, extra_dirs: tuple[str, ...] = ()):
        env = [p for p in os.environ.get("BSP_HOOKS_PATH", "").split(":") if p]
        dirs = [*env, *extra_dirs, str(Path.home() / ".config" / "bsp" / "hooks"), str(REPO_HOOKS)]
        self.dirs = [Path(d).expanduser() for d in dirs]

    def find(self, kind: str, name: str) -> Path:
        for d in self.dirs:
            p = d / kind / name
            if p.is_file() and os.access(p, os.X_OK):
                return p
        raise NotFound(f"no hook {kind}/{name}", hint=f"available: {', '.join(self.names(kind)) or '(none)'}; "
                                                       f"search path: {', '.join(map(str, self.dirs))}")

    def names(self, kind: str) -> list[str]:
        seen: dict[str, None] = {}
        for d in self.dirs:
            k = d / kind
            if k.is_dir():
                for p in sorted(k.iterdir()):
                    if p.is_file() and os.access(p, os.X_OK) and not p.name.startswith((".", "_")):
                        seen.setdefault(p.name)
        return list(seen)

    def run(self, kind: str, name: str, request: dict[str, Any], timeout: float = 120,
            log: Optional[Callable[[str], None]] = None) -> HookResult:
        path = self.find(kind, name)
        t0 = time.monotonic()
        proc = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "BSP_HOOK_KIND": kind, "BSP_HOOK_NAME": name})
        try:
            out, err = proc.communicate(json.dumps(request), timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise HardwareTimeout(f"hook {kind}/{name} timed out after {timeout:g}s") from None
        lines = [l for l in out.splitlines() if l.strip()]
        data: dict[str, Any] = {}
        if lines:
            try:
                parsed = json.loads(lines[-1])
                if isinstance(parsed, dict):
                    data, lines = parsed, lines[:-1]
            except ValueError:
                pass
        if log:
            for l in lines:
                log(f"{kind}/{name}: {l}")
        res = HookResult(name, proc.returncode, data, lines, round(time.monotonic() - t0, 2))
        if proc.returncode not in (0, 2):
            msg = data.get("error") or (err.strip().splitlines() or lines or ["(no output)"])[-1]
            raise HardwareError(f"hook {kind}/{name} failed (exit {proc.returncode}): {msg}",
                                hint=data.get("hint") or f"run it by hand: echo '<json>' | {path}",
                                details={"stderr": err[-1500:], "stdout": lines[-20:]})
        if not data and proc.returncode == 0:
            raise InvalidRequest(f"hook {kind}/{name} printed no JSON result on its last line",
                                 hint="hooks must end stdout with one JSON object")
        return res
