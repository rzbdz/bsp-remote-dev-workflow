"""Images the host stages (probe images, media-boot helpers) that a site does not keep in its tree.

An image in a SoC profile may say where it comes from and what it must hash to:

    images:
      helper:
        file: helper.bin
        sha256: <hex>                 # required for anything fetched
        source:                       # one of
          git: <repo url, ssh or https>
          ref: <branch, tag or commit>
          path: <file in the repository>
        # source: {url: https://...}  # or a plain download
        # source: {file: /some/path}  # or a local file

`bsp images sync` fetches each such image that no layer holds into the site's content-addressed
cache, `<site>/.blobs/<sha256>/<file>`, once: an image whose hash is already there is not fetched
again, and nothing is kept unless it hashes to its sha256. `deploy.sh` runs it before shipping a
site, so board hosts get the images with the site and never fetch anything themselves. Keep the
cache out of the site's git (`.blobs/` in its .gitignore); the list of images, in the site, is what
is versioned. A newer image is a new entry (or a new sha256 and ref) and another sync.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..core.errors import InvalidRequest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(source: dict[str, Any], dest: Path, cache: Path) -> None:
    if source.get("git"):
        repo, ref, path = source["git"], source.get("ref") or "HEAD", source.get("path")
        if not path:
            raise InvalidRequest(f"source {repo} needs `path`: the file in the repository")
        mirror = cache / "repos" / hashlib.sha256(repo.encode()).hexdigest()[:16]
        if not mirror.exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            _git(["clone", "--quiet", "--mirror", repo, str(mirror)])
        else:
            _git(["--git-dir", str(mirror), "fetch", "--quiet", "--prune", "origin", "+refs/*:refs/*"])
        data = subprocess.run(["git", "--git-dir", str(mirror), "show", f"{ref}:{path}"], capture_output=True, timeout=120)
        if data.returncode:
            raise InvalidRequest(f"{repo} has no {path} at {ref}: {data.stderr.decode(errors='replace').strip()[-200:]}")
        dest.write_bytes(data.stdout)
    elif source.get("url"):
        with urllib.request.urlopen(source["url"], timeout=60) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    elif source.get("file"):
        shutil.copyfile(Path(source["file"]).expanduser(), dest)
    else:
        raise InvalidRequest(f"unknown image source {source!r}", hint="one of {git, ref, path}, {url}, {file}")


def _git(args: list[str]) -> None:
    r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise InvalidRequest(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[-300:]}")


def plan(profiles, site: Path) -> list[dict[str, Any]]:
    """Every image of every SoC profile: where it comes from and whether this site has it."""
    out = []
    for soc, prof in sorted(profiles.profiles.items()):
        for key, img in sorted((prof.get("images") or {}).items()):
            out.append({"soc": soc, "image": key, "file": img.get("file"), "sha256": img.get("sha256"),
                        "source": img.get("source"), "path": img.get("path"),
                        "state": "present" if img.get("path") else "fetchable" if img.get("source") else "missing"})
    return out


def sync(profiles, site: Path, log: Callable[[str], None] = lambda line: None,
         cache: Path | None = None) -> dict[str, Any]:
    """Fetch the images no layer holds into <site>/.blobs/<sha256>/; each kept only if it hashes right."""
    cache = cache or Path.home() / ".cache" / "bsp"
    fetched, failed, present = [], [], []
    for item in plan(profiles, site):
        name = f"{item['soc']}/{item['image']}"
        if item["state"] == "present":
            present.append(name)
            continue
        if item["state"] == "missing":
            continue                      # nothing to fetch: a file the site puts there by hand
        want = str(item.get("sha256") or "").lower()
        if len(want) != 64:
            failed.append({"image": name, "error": "a fetched image needs its sha256"})
            continue
        dest = site / ".blobs" / want / item["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
            part = Path(tmp.name)
        try:
            _fetch(item["source"], part, cache)
            got = _sha256(part)
            if got != want:
                raise InvalidRequest(f"fetched {item['file']} hashes to {got}, not {want}")
            part.replace(dest)
            fetched.append(name)
            log(f"{name}: fetched {item['file']} ({dest.stat().st_size} bytes, sha256 {want[:16]}...)")
        except Exception as e:  # noqa: BLE001 - one failing source never stops the others
            part.unlink(missing_ok=True)
            failed.append({"image": name, "error": getattr(e, "message", str(e))})
            log(f"{name}: not fetched: {getattr(e, 'message', str(e))}")
    return {"fetched": fetched, "present": present, "failed": failed}
