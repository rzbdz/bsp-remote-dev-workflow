"""Console logins: `ssh bsp-<dut>@<host>` opens that DUT's serial console, no password.

Opt-in per host (`modules.dut.ssh_console.enabled`). The host keeps one system user per
DUT and reconciles the set with the DUT registry at startup and after every change, so
logins of removed or renamed boards disappear (and their open sessions end). Turning the
feature off removes every user, the group, the sshd drop-in, the sudo rule and the helpers.

How a login works:
  sshd (Match User <prefix>*): empty password allowed, ForceCommand, no forwarding
    -> login shell /usr/local/lib/bsp/console-shell (ignores any requested command)
    -> sudo -n /usr/local/lib/bsp/console-attach   (no arguments allowed by sudoers)
    -> console-attach (root) maps $SUDO_USER to a DUT via /etc/bsp/console-logins.json
       and execs `smtty <alias>`; Ctrl-A Ctrl-X ends the session.
Users are recognised by their GECOS field ("bsp-console <dut>"); other accounts are never
touched.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

MARK = "bsp-console "
GROUP = "bsp-console"
LIB = Path("/usr/local/lib/bsp")
SHELL = LIB / "console-shell"
ATTACH = LIB / "console-attach"
MAP = Path("/etc/bsp/console-logins.json")
SSHD = Path("/etc/ssh/sshd_config.d/50-bsp-console.conf")
SUDOERS = Path("/etc/sudoers.d/bsp-console")

SHELL_SRC = """#!/bin/sh
# bsp console login shell: always the DUT console, never a shell (args are ignored).
exec sudo -n {attach}
"""

ATTACH_SRC = r"""#!/usr/bin/env python3
# Runs as root via sudo for a bsp console login user; maps the user to its DUT console.
import json, os, sys, urllib.parse, urllib.request
user = os.environ.get("SUDO_USER", "")
try:
    m = json.load(open("{map}"))
except OSError:
    sys.exit("bsp: console logins are not configured on this host")
e = m.get("logins", {}).get(user)
if not e:
    sys.exit("bsp: no DUT console for user %r (the board may have been removed)" % user)
lease = ""
try:
    url = "http://127.0.0.1:%d/api/v1/dut/status?dut=%s" % (m.get("port", 8765), urllib.parse.quote(e["dut"]))
    st = json.load(urllib.request.urlopen(url, timeout=3)).get("result", {})
    l = st.get("lease") or {}
    lease = "leased by %s" % l["holder"] if l.get("holder") else "not leased"
except Exception:
    pass
sys.stderr.write("bsp: %s console on %s (%s) %s- Ctrl-A Ctrl-X to leave\r\n"
                 % (e["dut"], m.get("host", "?"), e["alias"], "[" + lease + "] " if lease else ""))
os.environ["HOME"] = m.get("home", "/root")
os.execv(e["smtty"], [e["smtty"], e["alias"]])
"""


def login_name(prefix: str, dut: str) -> str | None:
    """bsp-<dut> as a valid Linux user name (<= 32 chars), or None if it cannot be one."""
    n = re.sub(r"[^a-z0-9_-]", "-", f"{prefix}{dut}".lower())
    if not re.match(r"^[a-z_][a-z0-9_-]*$", n) or len(n) > 32:
        return None
    return n


class ConsoleLogins:
    def __init__(self, cfg: dict[str, Any] | None, host_id: str, port: int, smtty: str,
                 run: Callable[[list[str]], None] | None = None, root: Path = Path("/")):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.prefix = cfg.get("prefix", "bsp-")
        self.host_id, self.port = host_id, port
        self.smtty = shutil.which(smtty) or smtty
        self._run = run or self._sh
        self.root = root          # tests point this at a temp dir
        self.last: dict[str, Any] = {}

    # ---------------------------------------------------------------- system --
    @staticmethod
    def _sh(argv: list[str]) -> None:
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)

    def _p(self, p: Path) -> Path:
        return self.root / p.relative_to("/")

    def _ours(self) -> dict[str, str]:
        """{user: dut} of the console users that exist now."""
        out = {}
        for e in self._passwd():
            if e[4].startswith(MARK):
                out[e[0]] = e[4][len(MARK):]
        return out

    def _passwd(self) -> list[tuple]:
        if self.root != Path("/"):
            f = self._p(Path("/etc/passwd"))
            return [tuple(l.split(":")) for l in f.read_text().splitlines()] if f.exists() else []
        return [tuple(p) for p in pwd.getpwall()]

    def _write(self, path: Path, text: str, mode: int) -> bool:
        p = self._p(path)
        if p.exists() and p.read_text() == text:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
        return True

    def _remove(self, path: Path) -> bool:
        p = self._p(path)
        if p.exists():
            p.unlink()
            return True
        return False

    def _sshd_conf(self) -> str:
        return (f"# Managed by bsp-hostd (modules.dut.ssh_console). Removed when the feature is off.\n"
                f"Match User {self.prefix}*\n"
                f"    PasswordAuthentication yes\n"
                f"    PermitEmptyPasswords yes\n"
                f"    PermitTTY yes\n"
                f"    ForceCommand {SHELL}\n"
                f"    AllowTcpForwarding no\n"
                f"    AllowStreamLocalForwarding no\n"
                f"    AllowAgentForwarding no\n"
                f"    X11Forwarding no\n"
                f"    PermitTunnel no\n"
                f"    PermitUserRC no\n"
                f"Match all\n")

    def _reload_sshd(self) -> None:
        try:
            self._run(["sshd", "-t"])                  # never reload a config sshd rejects
        except Exception:
            self._remove(SSHD)
            raise
        self._run(["systemctl", "reload-or-restart", "ssh" if Path("/lib/systemd/system/ssh.service").exists()
                   or Path("/usr/lib/systemd/system/ssh.service").exists() else "sshd"])

    # ------------------------------------------------------------- reconcile --
    def desired(self, duts: dict[str, str]) -> tuple[dict[str, dict[str, str]], list[str]]:
        """duts {dut: console alias} -> ({user: {dut, alias}}, skipped duts)."""
        out, skipped = {}, []
        if not self.enabled:
            return out, skipped
        for dut, alias in sorted(duts.items()):
            n = login_name(self.prefix, dut)
            if not n or n in out:
                skipped.append(dut)
                continue
            out[n] = {"dut": dut, "alias": alias}
        return out, skipped

    def sync(self, duts: dict[str, str]) -> dict[str, Any]:
        want, skipped = self.desired(duts)
        have = self._ours()
        added, removed, changed = [], [], []
        for user in sorted(set(have) - set(want)):
            if self._has_procs(user):
                self._run(["pkill", "-KILL", "-u", user])      # end open console sessions
            self._run(["userdel", user])
            removed.append(user)
        if want:
            self._ensure_group()
        for user, e in sorted(want.items()):
            if user in have and have[user] == e["dut"]:
                continue
            if user in have:
                self._run(["usermod", "-c", MARK + e["dut"], user])
                changed.append(user)
                continue
            self._run(["useradd", "--system", "--no-create-home", "--home-dir", "/",
                       "--shell", str(SHELL), "--gid", GROUP, "--comment", MARK + e["dut"], user])
            self._run(["passwd", "-d", user])
            added.append(user)
        files_changed = self._files(want)
        if not want and GROUP in self._groups():
            self._run(["groupdel", GROUP])
        self.last = {"enabled": self.enabled, "logins": {u: e["dut"] for u, e in want.items()},
                     "added": added, "removed": removed, "renamed": changed, "skipped": skipped,
                     "config_changed": files_changed}
        return self.last

    def _files(self, want: dict[str, dict[str, str]]) -> bool:
        if not want:
            ch = [self._remove(p) for p in (SSHD, SUDOERS, SHELL, ATTACH, MAP)]
            if ch[0]:
                self._reload_sshd()
            return any(ch)
        mp = {"host": self.host_id, "port": self.port, "home": str(Path.home()),
              "logins": {u: {**e, "smtty": self.smtty} for u, e in want.items()}}
        ch = [self._write(SHELL, SHELL_SRC.format(attach=ATTACH), 0o755),
              self._write(ATTACH, ATTACH_SRC.replace("{map}", str(MAP)), 0o755),
              self._write(MAP, json.dumps(mp, indent=2) + "\n", 0o644),
              self._sudoers(f"%{GROUP} ALL=(root) NOPASSWD: {ATTACH} \"\"\n")]
        sshd_changed = self._write(SSHD, self._sshd_conf(), 0o644)
        if sshd_changed:
            self._reload_sshd()
        return any(ch) or sshd_changed

    def _sudoers(self, text: str) -> bool:
        """Install the sudo rule only after `visudo -c` accepts it (a bad file breaks sudo)."""
        p = self._p(SUDOERS)
        if p.exists() and p.read_text() == text:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / ".bsp-console.check"
        tmp.write_text(text)
        os.chmod(tmp, 0o440)
        try:
            self._run(["visudo", "-c", "-q", "-f", str(tmp)])
        except Exception:
            tmp.unlink()
            raise
        os.replace(tmp, p)
        return True

    def _ensure_group(self) -> None:
        if GROUP not in self._groups():
            self._run(["groupadd", "--system", GROUP])

    def _groups(self) -> set[str]:
        f = self._p(Path("/etc/group"))
        return {l.split(":")[0] for l in f.read_text().splitlines()} if f.exists() else set()

    def _has_procs(self, user: str) -> bool:
        if self.root != Path("/"):
            return False
        return subprocess.run(["pgrep", "-u", user], stdout=subprocess.DEVNULL).returncode == 0

    def view(self) -> list[dict[str, str]]:
        return [{"login": f"{u}@<this host>", "dut": d} for u, d in sorted(self._ours().items())]
