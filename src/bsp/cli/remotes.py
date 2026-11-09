"""Named bsp host instances ("remotes") for the CLI. Stdlib only.

Resolution order for which instance a command talks to (per invocation):
    --url URL  >  --remote/-r NAME[,NAME|all]  >  $BSP_URL  >  $BSP_REMOTE  >  default remote

Remotes = the site list (<site>/remotes.json, see core/site.py; the repo ships none),
then the hosts that announced themselves to a registry ("registry": URL in remotes.json, or
$BSP_REGISTRY; their addresses take precedence), overlaid by the user file ($BSP_CONFIG, else $XDG_CONFIG_HOME/bsp/remotes.json,
else ~/.config/bsp/remotes.json). The user file and its default (`bsp remote use`)
are a convenience for interactive shells only. Agents and parallel jobs should
pass -r/--url or set $BSP_REMOTE per process; --stateless ($BSP_STATELESS=1)
enforces that by ignoring the user file and refusing to fall back to a default.
Names match case-insensitively and by unique prefix (``-r lab`` -> ``lab-1``).
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

def _site_remotes() -> Path:
    from ..core.site import site_dir

    s = site_dir()
    return (s / "remotes.json") if s else Path("/nonexistent/remotes.json")


# a hostd on this machine is always a candidate (host mode)
LOCAL = {"remotes": {"local": {"url": "http://127.0.0.1:8765", "description": "bsp-hostd on this machine"}}}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class RemoteError(Exception):
    def __init__(self, message: str, hint: str | None = None, code: str = "remote"):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


@dataclass
class Remote:
    name: str
    url: str
    token: str | None = None
    description: str = ""
    source: str = "builtin"   # builtin | registry | user | env | flag
    fallback_urls: list[str] = field(default_factory=list)   # tried in order when `url` is unreachable
    ssh: str | None = None    # last resort: tunnel HTTP over ssh to this target (e.g. root@192.0.2.20)
    port: int = 8765

    @property
    def urls(self) -> list[str]:
        return [self.url, *self.fallback_urls]

    def transports(self) -> list[Any]:
        from .transport import HttpTransport, SshTransport

        out: list[Any] = [HttpTransport(u, self.token) for u in self.urls]
        if self.ssh:
            out.append(SshTransport(self.ssh, self.port, self.token))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "fallback_urls": self.fallback_urls, "ssh": self.ssh,
                "description": self.description, "source": self.source, "token": "***" if self.token else None}


def user_config_path() -> Path:
    if os.environ.get("BSP_CONFIG"):
        return Path(os.environ["BSP_CONFIG"])
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "bsp" / "remotes.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RemoteError(f"{path} is not valid JSON: {e}", hint=f"fix or delete {path}") from None


class RemoteStore:
    def __init__(self, user_path: Path | None = None, builtin_path: Path | None = None, stateless: bool = False):
        self.user_path = user_path or user_config_path()
        self.stateless = stateless
        site = _read(builtin_path or _site_remotes())
        self._builtin = {**site, "remotes": {**LOCAL["remotes"], **(site.get("remotes") or {})}}
        self._user = {} if stateless else _read(self.user_path)
        # a host registry (see the registry module): board hosts announce themselves there
        self.registry_url = (os.environ.get("BSP_REGISTRY") or self._user.get("registry")
                             or self._builtin.get("registry") or None)
        self.registry_error: str | None = None
        self._registered: dict[str, dict[str, Any]] | None = None

    def registered(self) -> dict[str, dict[str, Any]]:
        """Remote entries of the hosts that announced themselves to the registry (empty without one)."""
        if self._registered is None:
            self._registered = {}
            if self.registry_url:
                from .transport import HttpTransport, Unreachable

                try:
                    r = HttpTransport(self.registry_url).request("GET", "/api/v1/registry/list", timeout=8)
                    if not (r or {}).get("ok"):
                        raise ValueError((r or {}).get("error", {}).get("message") or "not a bsp registry")
                    self._registered = {e["host"]: e["remote"] for e in r["result"] if _NAME_RE.match(e["host"])}
                except (Unreachable, ValueError, KeyError, TypeError) as e:
                    self.registry_error = f"registry {self.registry_url}: {e}"
        return self._registered

    # -------------------------------------------------------------- view --
    def remotes(self) -> dict[str, Remote]:
        out: dict[str, Remote] = {}
        reg = {"remotes": self.registered()} if self.registry_url else {}
        for src, data in (("builtin", self._builtin), ("registry", reg), ("user", self._user)):
            for name, r in (data.get("remotes") or {}).items():
                if r is None:          # user file can hide a builtin with null
                    out.pop(name, None)
                    continue
                prev = out.get(name)
                if src == "registry" and prev:          # fresh addresses; keep what the site says otherwise
                    urls = [r["url"], *(r.get("fallback_urls") or []), *prev.urls]
                    r = {**r, "url": urls[0], "fallback_urls": list(dict.fromkeys(urls[1:])),
                         "token": prev.token, "ssh": prev.ssh or r.get("ssh"),
                         "description": prev.description or r.get("description", "")}
                out[name] = Remote(name, r["url"].rstrip("/"), r.get("token"), r.get("description", ""), src,
                                   [u.rstrip("/") for u in r.get("fallback_urls") or []],
                                   r.get("ssh"), int(r.get("port", 8765)))
        return out

    @property
    def default_name(self) -> str | None:
        if self.stateless:
            return None
        return self._user.get("default") or self._builtin.get("default")

    def lookup(self, name: str) -> Remote:
        rs = self.remotes()
        key = name.strip().lower()
        exact = [r for n, r in rs.items() if n.lower() == key]
        if exact:
            return exact[0]
        pref = [r for n, r in rs.items() if n.lower().startswith(key)]
        if len(pref) == 1:
            return pref[0]
        if len(pref) > 1:
            raise RemoteError(f"remote {name!r} is ambiguous: {', '.join(r.name for r in pref)}",
                              hint="type more of the name")
        close = difflib.get_close_matches(name, list(rs), n=3, cutoff=0.4)
        raise RemoteError(
            f"unknown remote {name!r}",
            hint=(f"did you mean: {', '.join(close)}? " if close else "")
            + f"known: {', '.join(sorted(rs))}; add one with `bsp remote add <name> <url>`",
        )

    def select(self, url: str | None = None, remote: str | None = None, token: str | None = None) -> list[Remote]:
        """Return the instance(s) a command should go to."""
        if url:
            return [Remote("url", url.rstrip("/"), token, source="flag")]
        spec = remote or (None if os.environ.get("BSP_URL") else os.environ.get("BSP_REMOTE"))
        if spec:
            if spec.strip().lower() == "all":
                targets = list(self.remotes().values())
                targets = [r for r in targets if r.name != "local"] or targets
            else:
                targets = [self.lookup(s) for s in spec.split(",") if s.strip()]
        elif os.environ.get("BSP_URL"):
            targets = [Remote("env", os.environ["BSP_URL"].rstrip("/"), source="env")]
        elif self.default_name:
            targets = [self.lookup(self.default_name)]
        elif self.stateless:
            raise RemoteError(
                "stateless mode: no instance given",
                hint=f"pass -r <name> (known: {', '.join(sorted(self.remotes()))}), --url <url>, or set BSP_REMOTE / BSP_URL",
            )
        else:
            raise RemoteError("no bsp instance selected", hint="use -r <remote>, --url, or `bsp remote use <name>`")
        if token:
            for r in targets:
                r.token = token
        return targets

    # ----------------------------------------------------------- mutate --
    def _save(self) -> None:
        if self.stateless:
            raise RemoteError("stateless mode: refusing to modify the user remote file",
                              hint="drop --stateless / BSP_STATELESS to manage saved remotes")
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.user_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._user, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.user_path)

    def add(self, name: str, url: str, token: str | None = None, description: str = "", default: bool = False,
            fallback_urls: list[str] | None = None, ssh: str | None = None) -> Remote:
        if not _NAME_RE.match(name) or name.lower() == "all":
            raise RemoteError(f"invalid remote name {name!r}", hint="letters/digits/_.- ; 'all' is reserved")
        for u in [url, *(fallback_urls or [])]:
            if not re.match(r"^https?://", u):
                raise RemoteError(f"invalid url {u!r}", hint="e.g. http://192.0.2.20:8765")
        entry: dict[str, Any] = {"url": url.rstrip("/")}
        if fallback_urls:
            entry["fallback_urls"] = [u.rstrip("/") for u in fallback_urls]
        if ssh:
            entry["ssh"] = ssh
        if token:
            entry["token"] = token
        if description:
            entry["description"] = description
        self._user.setdefault("remotes", {})[name] = entry
        if default:
            self._user["default"] = name
        self._save()
        return self.remotes()[name]

    def remove(self, name: str) -> Remote:
        r = self.lookup(name)
        rs = self._user.setdefault("remotes", {})
        if r.source == "builtin":
            rs[r.name] = None          # hide the builtin entry
        else:
            rs.pop(r.name, None)
            if r.name in (self._builtin.get("remotes") or {}):
                rs[r.name] = None
        if self._user.get("default") == r.name:
            self._user.pop("default")
        self._save()
        return r

    def use(self, name: str) -> Remote:
        r = self.lookup(name)
        self._user["default"] = r.name
        self._save()
        return r
