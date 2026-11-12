"""Hooks: replaceable executables with a JSON contract.

A hook is any executable file ``<hooks-dir>/<kind>/<name>`` (any language).

    stdin   one JSON object: the request (documented per kind in hooks/README.md)
    stdout  progress lines (free text, shown as they come; `{"progress": N}` sets a job's percent) and,
            as the LAST line, one JSON object: the result
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
import signal
import subprocess
import threading
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
    def __init__(self, extra_dirs: tuple[str, ...] = (), env: dict[str, str] | None = None):
        env_dirs = [p for p in os.environ.get("BSP_HOOKS_PATH", "").split(":") if p]
        dirs = [*env_dirs, *extra_dirs, str(Path.home() / ".config" / "bsp" / "hooks"), str(REPO_HOOKS)]
        self.dirs = [Path(d).expanduser() for d in dirs]
        # The host config's `env:` map: in the hook's process environment too, so shell hooks can
        # use `${NAME:-default}` as they always have.
        self.env = dict(env or {})

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
            log: Optional[Callable[[str], None]] = None,
            progress: Optional[Callable[[float], None]] = None) -> HookResult:
        """Run a hook. Its stdout lines reach `log` as they come (prefixed `<kind>/<name>: `), so a long hook shows
        what it does while it runs. A line that is a JSON object with a progress value (`{"progress": 42}` or
        `{"type": "progress", "value": 42}`, percent) goes to `progress` instead, and to `log` as `42%` when it
        changed. The last JSON object line is the result. On timeout the hook gets SIGTERM (so it can let go of
        a device), then SIGKILL."""
        path = self.find(kind, name)
        t0 = time.monotonic()
        proc = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True,
                                env={**os.environ, **self.env, "BSP_HOOK_KIND": kind, "BSP_HOOK_NAME": name,
                                     "PYTHONUNBUFFERED": "1"})
        err_chunks: list[str] = []
        timed_out = threading.Event()

        def feed() -> None:
            try:
                proc.stdin.write(json.dumps(request))
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass                                    # a hook may exit without reading its request

        def drain() -> None:
            for chunk in iter(lambda: proc.stderr.read(8192), ""):
                err_chunks.append(chunk)
                if sum(map(len, err_chunks)) > 65536:
                    del err_chunks[0]

        def expire() -> None:
            timed_out.set()
            _stop(proc)

        helpers = [threading.Thread(target=f, daemon=True) for f in (feed, drain)]
        for t in helpers:
            t.start()
        watchdog = threading.Timer(timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        prefix = f"{kind}/{name}: "
        lines: list[str] = []
        held: Optional[dict[str, Any]] = None          # the latest JSON object line: the result if nothing follows
        shown: list[Optional[int]] = [None]

        def emit(line: str) -> None:
            lines.append(line)
            if log:
                log(prefix + line)

        def use(obj: dict[str, Any], raw: str) -> None:
            """A JSON object line that turned out not to be the last one."""
            pct = _progress_of(obj)
            if pct is None:
                emit(raw)
                return
            if progress:
                progress(pct)
            msg = obj.get("msg") or obj.get("message")
            if int(pct) != shown[0] or msg:
                shown[0] = int(pct)
                emit(f"{pct:g}%" + (f" {msg}" if msg else ""))

        raw_held = ""
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                if held is not None:
                    use(held, raw_held)
                    held = None
                obj = _json_object(line)
                if obj is not None:
                    held, raw_held = obj, line
                else:
                    emit(line)
            proc.wait()
        finally:
            watchdog.cancel()
            for t in helpers:
                t.join(5)
        if timed_out.is_set():
            raise HardwareTimeout(f"hook {kind}/{name} timed out after {timeout:g}s",
                                  hint="give it more time (a timeout option of the command, or the recipe step's "
                                       "`timeout`), and check what it printed last",
                                  details={"stdout": lines[-20:], "stderr": "".join(err_chunks)[-1500:]})
        err = "".join(err_chunks)
        data: dict[str, Any] = held if held is not None else {}
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


def _json_object(line: str) -> Optional[dict[str, Any]]:
    s = line.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _progress_of(obj: dict[str, Any]) -> Optional[float]:
    """The percent a progress line carries: `{"progress": N}` or `{"type": "progress", "value": N}`."""
    v = obj.get("progress") if "progress" in obj else obj.get("value") if obj.get("type") == "progress" else None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return max(0.0, min(100.0, float(v)))


def _stop(proc: subprocess.Popen, grace: float = 10) -> None:
    """SIGTERM to the hook and what it started, then SIGKILL if they are still there after `grace` seconds."""
    def signal_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
    signal_group(signal.SIGTERM)
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    signal_group(signal.SIGKILL)
