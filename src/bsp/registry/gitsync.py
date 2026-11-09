"""git-registry: the registry's SDK catalog kept in a git repository as well.

The registry's site directory is a clone of the catalog repository (`modules.registry.git`).
Every catalog write is written into it (`sdk/<kind>/<id>.yaml`, or removed), committed with the
change's author and message and a `Catalog-Revision:` trailer, and pushed; a worker thread does
this in order, so a write never waits for the network. When the remote moved, the worker fetches,
rebases and pushes again.

Commits pushed to the repository by anyone else (a push, an edit on the web) are pulled every
`pull_s` seconds or on request: each SDK document they change is validated against the whole
catalog and written to the catalog as that commit's author, so hosts get it like any other
change; one that does not validate is reported (`bsp catalog status`) and not applied. Other files
in the repository (hosts, SoC profiles, remotes) are simply updated in the clone.
"""

from __future__ import annotations

import logging
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..sdk.catalog import CATALOG_KINDS
from .catalog import CatalogStore, ref_of

log = logging.getLogger(__name__)
TRAILER = "Catalog-Revision"


class GitError(Exception):
    pass


class GitMirror:
    def __init__(self, store: CatalogStore, workdir: Path, cfg: dict[str, Any], bus=None):
        self.store, self.dir, self.bus = store, Path(workdir), bus
        self.remote = cfg.get("remote") or None
        self.branch = cfg.get("branch", "main")
        self.pull_s = float(cfg.get("pull_s", 60))
        self.name = cfg.get("committer", "bsp registry")
        self.email = cfg.get("email", "registry@bsp.invalid")
        self.queue: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        self.state: dict[str, Any] = {"head": None, "pushed": None, "pending": 0, "last_error": None,
                                      "last_pull": None, "rejected": []}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------- git --
    def git(self, *args: str, check: bool = True, env: dict[str, str] | None = None, timeout: float = 120,
            raw: bool = False) -> str:
        """Run git in the clone; `raw` keeps the output exactly (file contents), otherwise it is stripped."""
        import os

        r = subprocess.run(["git", "-C", str(self.dir), *args], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
        if check and r.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout if raw else r.stdout.strip()

    def ensure(self) -> None:
        if not (self.dir / ".git").exists():
            raise GitError(f"{self.dir} is not a git checkout of the catalog repository "
                           f"(clone {self.remote or 'it'} there first)")
        if self.remote and self.git("remote", "get-url", "origin", check=False) != self.remote:
            self.git("remote", "set-url", "origin", self.remote, check=False) or self.git("remote", "add", "origin", self.remote, check=False)
        self.state["head"] = self.git("rev-parse", "--short", "HEAD", check=False) or None

    # ---------------------------------------------------- registry -> git --
    def record(self, kind: str, ident: str, text: str | None, author: str, message: str, rev: int) -> None:
        """Queue a catalog write to be committed and pushed (called after the store has it)."""
        self.queue.put({"kind": kind, "id": ident, "text": text, "author": author, "message": message, "rev": rev})
        self.state["pending"] = self.queue.qsize()

    def _commit(self, job: dict[str, Any]) -> bool:
        path = self.dir / "sdk" / job["kind"] / f"{job['id']}.yaml"
        rel = str(path.relative_to(self.dir))
        if job["text"] is None:
            if not path.exists():
                return False
            self.git("rm", "-q", "--", rel)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_text() == job["text"]:
                return False
            path.write_text(job["text"])
            self.git("add", "--", rel)
        if not self.git("diff", "--cached", "--name-only", check=False):
            return False
        who = job["author"] or "unknown"
        email = re.sub(r"[^A-Za-z0-9._+-]", "-", who) + "@bsp.invalid"
        ref = ref_of(job["kind"], job["id"])
        subject = job["message"] or f"{'delete' if job['text'] is None else 'update'} {ref}"
        msg = f"{subject if subject.startswith(ref + ':') else f'{ref}: {subject}'}\n\n{TRAILER}: {job['rev']}\n"
        self.git("commit", "-q", "-m", msg, env={"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": email,
                                                   "GIT_COMMITTER_NAME": self.name, "GIT_COMMITTER_EMAIL": self.email})
        return True

    def push(self) -> None:
        if not self.remote:
            return
        for attempt in range(3):
            r = subprocess.run(["git", "-C", str(self.dir), "push", "-q", "origin", f"HEAD:{self.branch}"],
                               capture_output=True, text=True, timeout=120, env={"GIT_TERMINAL_PROMPT": "0",
                                                                                   **__import__("os").environ})
            if r.returncode == 0:
                self.state["pushed"] = self.git("rev-parse", "--short", "HEAD")
                self.state["last_error"] = None
                return
            # someone pushed meanwhile: take their commits in first (validated), then ours on top
            self.pull()
        raise GitError(f"push to {self.remote} failed: {(r.stderr or r.stdout).strip()[-300:]}")

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.queue.get(timeout=self.pull_s if self.remote else 3600)
            except queue.Empty:
                job = None
            try:
                with self._lock:
                    if job:
                        committed = self._commit(job)
                        while not self.queue.empty():                 # take everything queued in one push
                            nxt = self.queue.get_nowait()
                            if nxt is None:
                                break
                            committed = self._commit(nxt) or committed
                        if committed:
                            self.push()
                    elif self.remote:
                        self.pull()
                    self.state["head"] = self.git("rev-parse", "--short", "HEAD", check=False) or None
            except Exception as e:  # noqa: BLE001 - reported in status; the next round tries again
                self.state["last_error"] = str(e)
                log.warning("git-registry: %s", e)
            self.state["pending"] = self.queue.qsize()

    # ---------------------------------------------------- git -> registry --
    def pull(self) -> dict[str, Any]:
        """Fetch the remote; apply SDK documents changed by commits the registry did not make, then move to the
        remote's head (rebasing the registry's own unpushed commits on top)."""
        if not self.remote:
            return {"applied": [], "rejected": []}
        self.git("fetch", "-q", "origin", self.branch)
        theirs = f"origin/{self.branch}"
        incoming = self.git("rev-list", "--reverse", f"HEAD..{theirs}", check=False).split()
        applied, rejected = [], []
        for sha in incoming:
            body = self.git("show", "-s", "--format=%an%x00%s%x00%B", sha)
            author, subject, full = (body.split("\0") + ["", "", ""])[:3]
            if f"{TRAILER}:" in full:
                continue                                          # the registry's own commit, already in the store
            for line in self.git("diff-tree", "--no-commit-id", "--name-status", "-r", sha).splitlines():
                status, _, path = line.partition("\t")
                m = re.fullmatch(r"sdk/(" + "|".join(CATALOG_KINDS) + r")/([A-Za-z0-9][A-Za-z0-9_.-]*)\.yaml", path)
                if not m:
                    continue
                kind, ident = m.groups()
                ref = ref_of(kind, ident)
                text = None if status.startswith("D") else self.git("show", f"{sha}:{path}", raw=True)
                try:
                    cur = self.store._current(kind, ident)
                    res = self.store._write(kind, ident, text, cur["rev"] if cur else 0, True, f"git:{author}",
                                            f"{subject} (git {sha[:10]})", False)
                    applied.append({"ref": ref, "rev": res["rev"], "commit": sha[:10], "author": author})
                except Exception as e:  # noqa: BLE001 - reported; the catalog keeps what it had
                    error = getattr(e, "message", str(e))
                    rejected.append({"ref": ref, "commit": sha[:10], "author": author, "error": error})
                    self._keep(kind, ident, sha, path, author, error)
        if incoming:
            if self.git("rev-list", f"{theirs}..HEAD", check=False):
                self.git("rebase", "-q", theirs)
            else:
                self.git("merge", "-q", "--ff-only", theirs)
        self.state["last_pull"] = time.time()
        if rejected:
            self.state["rejected"] = (self.state["rejected"] + rejected)[-50:]
            log.warning("git-registry: not applied: %s", rejected)
        if (applied or rejected) and self.bus:
            self.bus.publish("catalog.git", applied=applied, rejected=rejected)
        return {"applied": applied, "rejected": rejected, "commits": len(incoming)}

    def _keep(self, kind: str, ident: str, sha: str, path: str, author: str, error: str) -> None:
        """A pushed change that does not validate: pin the document as it was before it, as a registry revision, so
        neither the catalog nor (through the clone, which is also the site) the files take the bad version. The pinned
        revision is committed back, which reverts the change in the repository with the reason."""
        try:
            before = self.git("show", f"{sha}^:{path}", check=False, raw=True) or None
            cur = self.store._current(kind, ident)
            if cur and cur["text"] == before:
                return
            self.store._write(kind, ident, before, cur["rev"] if cur else 0, True, "registry",
                              f"revert git {sha[:10]} by {author}: it does not validate ({error[:200]})", False)
        except Exception as e:  # noqa: BLE001 - reported with the rejection
            log.warning("git-registry: could not pin %s after a bad commit: %s", ref_of(kind, ident), e)

    def pull_now(self) -> dict[str, Any]:
        with self._lock:
            return self.pull()

    # --------------------------------------------------------------- run --
    def start(self) -> "GitMirror":
        self.ensure()
        threading.Thread(target=self._worker, name="git-registry", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.queue.put(None)

    def status(self) -> dict[str, Any]:
        return {"remote": self.remote, "branch": self.branch, "dir": str(self.dir), **self.state}
