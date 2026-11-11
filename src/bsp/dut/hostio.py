"""Host-side adapters: fastboot CLI, serial-mux (smtty + its log), artifact pulls."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..core.errors import Conflict, HardwareError, HardwareTimeout, InvalidRequest

Log = Callable[[str], None]


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HardwareTimeout(f"`{' '.join(argv[:4])} ...` timed out after {timeout:g}s") from None
    except FileNotFoundError:
        raise HardwareError(f"{argv[0]} not found on this host", hint="install it or set its path in the host config") from None


# ------------------------------------------------------------- fastboot --
class Fastboot:
    """fastboot CLI bound to one USB path (multi-DUT safe: always `-s usb:<path>`)."""

    def __init__(self, usb: str, binary: str = "fastboot"):
        self.usb = usb
        self.binary = binary

    def devices(self) -> list[dict[str, str]]:
        r = _run([self.binary, "devices", "-l"], 10)
        out, entries, cur = (r.stdout + r.stderr).splitlines(), [], None
        for line in out:
            if not line.strip():
                continue
            if line.startswith(" ") and cur is not None and "usb:" in line:
                cur["usb"] = line.strip().split()[0]
                continue
            parts = line.split(None, 1)
            cur = {"serial": parts[0], "product": parts[1].strip() if len(parts) > 1 else "", "usb": ""}
            m = re.search(r"(usb:\S+)", line)
            if m:
                cur["usb"] = m.group(1)
            entries.append(cur)
        return entries

    def state(self) -> dict[str, str] | None:
        """This DUT's fastboot entry, or None if it is not on the bus."""
        for d in self.devices():
            if d["usb"] == self.usb:
                return d
        return None

    def cmd(self, *args: str, timeout: float = 120) -> str:
        r = _run([self.binary, "-s", self.usb, *args], timeout)
        out = (r.stdout + r.stderr).strip()   # fastboot reports on stderr
        if r.returncode != 0 or "FAILED" in out:
            raise HardwareError(f"fastboot {' '.join(args)} failed: {out[-300:]}",
                                hint="check `bsp dut status` (is the board in the expected stage?)")
        return out

    def getvar(self, name: str) -> str:
        out = self.cmd("getvar", name, timeout=15)
        m = re.search(rf"^{re.escape(name)}:\s*(.*)$", out, re.M)
        return m.group(1).strip() if m else out


# serial-mux's log lines start with the time they were read
_STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")


# --------------------------------------------------------------- serial --
class SerialConsole:
    """serial-mux alias: commands via `smtty --send/--wait`, passive reads from its
    per-day log (cumulative, append-only) without injecting keystrokes."""

    def __init__(self, alias: str, smtty: str = "smtty", log_dir: str = "~/.serial-mux/logs"):
        self.alias = alias
        self.smtty = smtty
        self.log_dir = Path(log_dir).expanduser() / alias

    def _log_path(self, day: date | None = None) -> Path:
        return self.log_dir / f"{(day or date.today()).isoformat()}.log"

    def mark(self) -> tuple[Path, int]:
        p = self._log_path()
        return p, (p.stat().st_size if p.exists() else 0)

    def read_since(self, mark: tuple[Path, int]) -> str:
        path, off = mark
        chunks = []
        if path.exists():
            with open(path, "rb") as f:
                f.seek(off)
                chunks.append(f.read())
        today = self._log_path()
        if today != path and today.exists():          # day rollover
            chunks.append(today.read_bytes())
        return b"".join(chunks).decode("utf-8", errors="replace")

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        files = sorted(self.log_dir.glob("*.log"))
        out: list[str] = []
        for p in reversed(files[-2:]):
            out = p.read_bytes().decode("utf-8", errors="replace").splitlines() + out
            if len(out) >= lines * 4:
                break
        if grep:
            rx = re.compile(grep)
            out = [l for l in out if rx.search(l)]
        return out[-lines:]

    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False) -> dict[str, Any]:
        """Log lines stamped from `since` to `until` (`YYYY-MM-DD HH:MM:SS`, either may be None), across the day files.
        A line without a stamp of its own goes with the one before it. `grep` keeps matching lines (case-blind text, or
        a regex); at most `limit` lines are returned, the last ones (or the first, with `first`), and `truncated` says
        when some were left out. `matched` counts every line that matched."""
        lo, hi = (since or "")[:19], (until or "9999")[:19]
        day_lo, day_hi = lo[:10] or "0000", hi[:10]
        if grep and regex:
            rx = re.compile(grep)
            keep = lambda line: bool(rx.search(line))  # noqa: E731
        elif grep:
            needle = grep.casefold()
            keep = lambda line: needle in line.casefold()  # noqa: E731
        else:
            keep = lambda line: True  # noqa: E731
        out: list[str] = []
        matched = 0
        for p in sorted(self.log_dir.glob("*.log")):
            if not (day_lo <= p.stem <= day_hi):
                continue
            inside = False
            for line in p.read_bytes().decode("utf-8", errors="replace").splitlines():
                stamp = _STAMP.match(line)
                if stamp:
                    inside = lo <= stamp.group(1) <= hi
                if not inside or not keep(line):
                    continue
                matched += 1
                if not first or len(out) < limit:
                    out.append(line)
                    if not first and len(out) > limit:
                        del out[: len(out) - limit]
        return {"lines": out, "matched": matched, "truncated": matched > len(out)}

    def nudge(self) -> None:
        """Press Enter on the console (best effort). The log is line-based, so a prompt
        printed without a newline (`~ # `) only shows up once something follows it."""
        try:
            _run([self.smtty, self.alias, "--send", " "], timeout=10)
        except Exception:  # noqa: BLE001 - a missed nudge only delays the wait
            pass

    def wait(self, mark: tuple[Path, int], pattern: str | None, fail: str | None,
             timeout: float, stall: float | None = None, nudge: float | None = None) -> str:
        """Wait for `pattern` in the log after `mark`. Raises on `fail` match or timeout.
        `nudge`: press Enter whenever the console has been quiet that many seconds."""
        rx = re.compile(pattern) if pattern else None
        frx = re.compile(fail) if fail else None
        deadline = time.monotonic() + timeout
        last_size, last_move = -1, time.monotonic()
        last_nudge = last_move
        while True:
            text = self.read_since(mark)
            if frx and frx.search(text):
                m = frx.search(text)
                ctx = text[max(0, m.start() - 400): m.end() + 200]
                raise HardwareError(f"console reported failure /{fail}/", details={"console": ctx[-800:]})
            if rx is None or rx.search(text):
                return text
            if len(text) != last_size:
                last_size, last_move = len(text), time.monotonic()
            now = time.monotonic()
            if nudge and now - max(last_move, last_nudge) > nudge:
                self.nudge()
                last_nudge = time.monotonic()
            if stall and now - last_move > stall:
                raise HardwareTimeout(f"console went silent for {stall:g}s while waiting for /{pattern}/",
                                      details={"console_tail": text[-800:]})
            if now > deadline:
                raise HardwareTimeout(f"/{pattern}/ not seen on console within {timeout:g}s",
                                      hint="see `bsp dut log <dut>`; the board may be in another stage",
                                      details={"console_tail": text[-800:]})
            time.sleep(0.25)

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        """Send one line, wait for `expect`, return the console text it produced."""
        if not command.strip():
            raise InvalidRequest("empty console command", hint="pass a shell command such as `dmesg | tail`")
        mark = self.mark()
        r = _run([self.smtty, self.alias, "--send", command, "--wait", expect, "--timeout", str(int(timeout))],
                 timeout + 15)
        text = self.read_since(mark)
        if r.returncode == 2:
            raise HardwareTimeout(f"prompt /{expect}/ not seen within {timeout:g}s after `{command}`",
                                  hint="the board may not be booted to a shell: `bsp dut status`",
                                  details={"output": (r.stdout or text)[-2000:]})
        if r.returncode not in (0,):
            raise HardwareError(f"smtty failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}",
                                hint=f"is serial-mux alias '{self.alias}' running? `serial-mux list` on the host")
        return {"command": command, "output": _strip_console(r.stdout, command), "raw": r.stdout[-8000:]}


_TS = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ?")


def _strip_console(out: str, command: str) -> str:
    lines = [_TS.sub("", l).rstrip("\r") for l in out.splitlines()]
    # drop the echoed command line and the trailing prompt
    if lines and command.strip() and command.strip() in lines[0]:
        lines = lines[1:]
    while lines and re.fullmatch(r"\s*(~ )?#\s*", lines[-1]):
        lines.pop()
    return "\n".join(lines)


# ------------------------------------------------------------ artifacts --
class ArtifactStore:
    """Content-addressed cache of build outputs pulled from build servers over SSH.
    Boot always reads local files (never CIFS)."""

    SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def pull(self, ssh: str, files: dict[str, str], log: Log) -> dict[str, dict[str, Any]]:
        """files: role -> absolute remote path. Returns role -> {path, sha256, size, mtime}."""
        globs = {r: p for r, p in files.items() if any(c in p for c in "*?[")}
        if globs:
            script = "; ".join(f"echo {shlex.quote(r)} $(ls -t {p} 2>/dev/null | head -1)" for r, p in globs.items())
            rg = _run(["ssh", *self.SSH_OPTS, ssh, script], 60)
            for line in rg.stdout.splitlines():
                role, _, found = line.partition(" ")
                if found.strip():
                    files = {**files, role: found.strip()}
        paths = list(files.values())
        script = "for f in " + " ".join(shlex.quote(p) for p in paths) + \
                 '; do if [ -f "$f" ]; then echo "$(sha256sum "$f" | cut -d" " -f1) $(stat -c "%s %Y" "$f") $f"; else echo "MISSING - - $f"; fi; done'
        r = _run(["ssh", *self.SSH_OPTS, ssh, script], 120)
        if r.returncode != 0:
            raise HardwareError(f"cannot reach build server {ssh}: {r.stderr.strip()[-300:]}",
                                hint="this host needs an SSH key accepted by the build server")
        meta = {}
        for line in r.stdout.splitlines():
            sha, size, mtime, path = line.split(" ", 3)
            meta[path] = (sha, size, mtime)
        out: dict[str, dict[str, Any]] = {}
        for role, path in files.items():
            sha, size, mtime = meta.get(path, ("MISSING", "-", "-"))
            if sha == "MISSING":
                raise InvalidRequest(f"artifact '{role}' missing on {ssh}: {path}",
                                     hint="build it first: `bsp sdk build <sdk> <component>`")
            local = self.root / sha / Path(path).name
            if local.exists():
                log(f"artifact {role}: {Path(path).name} cached ({int(size):,} B, sha {sha[:12]})")
            else:
                local.parent.mkdir(parents=True, exist_ok=True)
                tmp = local.with_suffix(local.suffix + ".part")
                t0 = time.monotonic()
                rr = _run(["scp", "-q", *self.SSH_OPTS, f"{ssh}:{path}", str(tmp)], 600)
                if rr.returncode != 0:
                    raise HardwareError(f"scp {path} failed: {rr.stderr.strip()[-300:]}")
                got = _sha256(tmp)
                if got != sha:
                    tmp.unlink(missing_ok=True)
                    raise HardwareError(f"checksum mismatch pulling {path} (changed during copy?)",
                                        hint="retry once the build has finished writing it")
                os.replace(tmp, local)
                log(f"artifact {role}: pulled {Path(path).name} ({int(size):,} B in {time.monotonic() - t0:.1f}s, sha {sha[:12]})")
            out[role] = {"path": str(local), "remote": path, "sha256": sha, "size": int(size),
                         "mtime": int(mtime), "age_s": int(time.time()) - int(mtime)}
        return out

    # ------------------------------------------------- uploaded files --
    def find(self, sha256: str) -> Path | None:
        """The cached file with this content, whichever way it arrived (pulled or uploaded)."""
        if not SHA256.fullmatch(sha256 or ""):
            return None
        d = self.root / sha256
        if not d.is_dir():
            return None
        for p in sorted(d.iterdir()):
            if p.is_file() and not p.name.endswith(".part"):
                return p
        return None

    def get(self, sha256: str) -> Path:
        p = self.find(sha256)
        if p is None:
            raise InvalidRequest(f"no file with sha256 {sha256[:16]}... on this host",
                                 hint="upload it first: `bsp dut stage DUT FILE` does that for you "
                                      "(PUT /api/v1/artifacts/<sha256>)")
        return p

    def upload(self, sha256: str, name: str) -> "Upload":
        """Start storing an uploaded file under its sha256: `write()` the body, then `commit()`."""
        if not SHA256.fullmatch(sha256 or ""):
            raise InvalidRequest(f"not a sha256: {sha256!r}", hint="64 lowercase hex digits")
        return Upload(self, sha256, _clean_name(name))

    def adopt(self, tmp: Path, sha256: str, name: str) -> Path:
        """Move a file whose content was checked to hash to `sha256` into the cache (same filesystem)."""
        have = self.find(sha256)
        if have is not None:
            tmp.unlink(missing_ok=True)
            return have
        if not SHA256.fullmatch(sha256 or ""):
            raise InvalidRequest(f"not a sha256: {sha256!r}", hint="64 lowercase hex digits")
        final = self.root / sha256 / _clean_name(name)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        return final

    def put(self, sha256: str, name: str, chunks) -> dict[str, Any]:
        up = self.upload(sha256, name)
        try:
            for chunk in chunks:
                up.write(chunk)
            return up.commit()
        finally:
            up.abort()


def _clean_name(name: str) -> str:
    """A cached file's name: the base name, with only safe characters, never hidden."""
    name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(name or "file").name)[:120] or "file"
    return "_" + name if name.startswith(".") else name


class Upload:
    """One file being uploaded into an ArtifactStore. Its content is hashed as it arrives; a body
    that does not match the announced sha256 is discarded. A file already cached is not rewritten."""

    def __init__(self, store: ArtifactStore, sha256: str, name: str):
        self.sha256, self.name, self.size = sha256, name, 0
        self.have = store.find(sha256)
        self._h = hashlib.sha256()
        self._f = None
        self.tmp: Path | None = None
        if self.have is None:
            d = store.root / sha256
            d.mkdir(parents=True, exist_ok=True)
            self.final = d / name
            self.tmp = d / f"{name}.{os.getpid()}.{time.monotonic_ns()}.part"
            self._f = open(self.tmp, "wb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self._f is not None:
            self._h.update(chunk)
            self._f.write(chunk)

    def commit(self) -> dict[str, Any]:
        if self.have is not None:
            return {"sha256": self.sha256, "path": str(self.have), "size": self.have.stat().st_size, "stored": False}
        self._f.close()
        got = self._h.hexdigest()
        if got != self.sha256:
            raise InvalidRequest(f"upload does not match its sha256 (got {got[:16]}..., {self.size:,} B)",
                                 hint="the file changed while it was sent, or the transfer was cut; send it again")
        if not self.size:
            raise InvalidRequest("refusing an empty file")
        os.replace(self.tmp, self.final)
        self.tmp = None
        return {"sha256": self.sha256, "path": str(self.final), "size": self.size, "stored": True}

    def abort(self) -> None:
        if self._f is not None and not self._f.closed:
            self._f.close()
        if self.tmp is not None:
            self.tmp.unlink(missing_ok=True)
            self.tmp = None


SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(cond: bool, msg: str, **kw: Any) -> None:
    if not cond:
        raise Conflict(msg, **kw)
