"""Files the registry shares besides SDK documents: SoC profiles, hooks, and the images they stage.

The registry's site (a clone of the catalog repository) holds `socs/` and `hooks/`. Board hosts and
CLIs that follow the registry get them as a layer above their own site, so every board and every
compile machine uses the same profiles, media-boot steps and hook scripts:

    GET /api/v1/registry/files            manifest: {digest, files: [{path, sha256, size, mode, data?}]}
    GET /api/v1/registry/blobs/<sha256>   one file by its hash (large files, and images from <site>/.blobs)

Shared are the SoC profiles (`socs/*/soc.yaml`) and every hook; images go only by hash, when a
profile lists one with its `sha256` (a file next to the profile, or one `bsp images sync` fetched
into <site>/.blobs). Small files come inline (base64), the rest by hash, and nothing is kept that
does not hash right. Hook scripts are code that hosts run, so these routes need
the registry's token when it has one, and the files only ever come from the catalog repository
(pushed through git, not through the open catalog API).
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SHARED = ("socs", "hooks")
INLINE_MAX = 64 * 1024


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SharedFiles:
    """The registry side: what its site shares, hashed once per change of size and mtime."""

    def __init__(self, site: Path):
        self.site = Path(site)

    def entries(self) -> List[Dict[str, Any]]:
        out = []
        hooks = self.site / "hooks"
        files = sorted(self.site.glob("socs/*/soc.yaml"))
        if hooks.is_dir():
            files += sorted(p for p in hooks.rglob("*") if p.is_file())
        for p in files:
            rel = p.relative_to(self.site)
            if any(part.startswith(".") for part in rel.parts):
                continue
            out.append({"path": rel.as_posix(), "sha256": _sha256(p), "size": p.stat().st_size,
                        "mode": "x" if os.access(p, os.X_OK) else "", "_file": p})
        return out

    def images(self) -> Dict[str, Path]:
        """Images the shared profiles list by sha256, by hash: next to the profile, or in <site>/.blobs."""
        out = {}
        for img in _images(self.site):
            sha = img["sha256"]
            for p in (img["dir"] / img["file"], self.site / ".blobs" / sha / img["file"]):
                if p.is_file() and _sha256(p) == sha:
                    out[sha] = p
                    break
        return out

    def manifest(self) -> Dict[str, Any]:
        files = []
        for e in self.entries():
            item = {k: v for k, v in e.items() if not k.startswith("_")}
            if e["size"] <= INLINE_MAX:
                item["data"] = base64.b64encode(e["_file"].read_bytes()).decode()
            files.append(item)
        return {"digest": digest(files), "files": files}

    def digest(self) -> str:
        return digest(self.entries())

    def blob(self, sha256: str) -> Optional[Path]:
        """A shared file or a fetched site image with this hash."""
        sha = sha256.lower()
        image = self.images().get(sha)
        if image:
            return image
        for e in self.entries():
            if e["sha256"] == sha:
                return e["_file"]
        return None


def digest(files: List[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda f: f["path"]):
        h.update(f"{f['path']}\0{f['sha256']}\0{f.get('mode', '')}\n".encode())
    return h.hexdigest()


def layer_digest(dest: Path) -> str:
    try:
        return (Path(dest) / "DIGEST").read_text().strip()
    except OSError:
        return ""


def _images(root: Path, beside: bool = True) -> List[Dict[str, Any]]:
    """Images the SoC profiles under `root` list with a sha256 (with `beside` False: only those not next to it)."""
    import yaml

    out = []
    for prof in sorted((root / "socs").glob("*/soc.yaml")):
        try:
            data = yaml.safe_load(prof.read_text()) or {}
        except Exception:  # noqa: BLE001 - a broken profile is reported when it is loaded
            continue
        for img in (data.get("images") or {}).values():
            sha, name = str((img or {}).get("sha256") or "").lower(), (img or {}).get("file")
            if len(sha) == 64 and name and (beside or not (prof.parent / name).is_file()):
                out.append({"sha256": sha, "file": name, "dir": prof.parent})
    return out


def materialize(manifest: Dict[str, Any], dest: Path, fetch: Callable[[str], bytes],
                log: Callable[[str], None] = lambda line: None) -> Dict[str, Any]:
    """Write the shared files as a layer at `dest` (socs/, hooks/, .blobs/), in one step. `fetch(sha256)` gets a
    file that is not inline. Images kept from before are reused; nothing is kept that does not hash right."""
    dest = Path(dest)
    tmp = dest.with_name(dest.name + f".new-{time.monotonic_ns()}")
    tmp.mkdir(parents=True)
    old_blobs = dest / ".blobs"

    def get(sha: str) -> bytes:
        for p in (old_blobs / sha).glob("*") if (old_blobs / sha).is_dir() else []:
            if _sha256(p) == sha:
                return p.read_bytes()
        data = fetch(sha)
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"registry sent a file for {sha[:16]}... that hashes differently")
        return data

    try:
        for f in manifest.get("files", []):
            rel = Path(f["path"])
            if rel.is_absolute() or ".." in rel.parts or rel.parts[0] not in SHARED:
                raise ValueError(f"registry sent a file outside {SHARED}: {f['path']}")
            data = base64.b64decode(f["data"]) if "data" in f else get(f["sha256"])
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise ValueError(f"{f['path']} does not hash to {f['sha256']}")
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            if f.get("mode") == "x":
                p.chmod(0o755)
        fetched, missing = [], []
        for img in _images(tmp, beside=False):
            target = tmp / ".blobs" / img["sha256"] / img["file"]
            if target.exists():
                continue
            try:
                data = get(img["sha256"])
            except Exception as e:  # noqa: BLE001 - reported; a profile whose image is missing says so itself
                missing.append({"file": img["file"], "sha256": img["sha256"], "error": str(e)})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            fetched.append(img["file"])
        (tmp / "DIGEST").write_text(manifest.get("digest", ""))
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    old = dest.with_name(dest.name + f".old-{time.monotonic_ns()}")
    if dest.exists():
        dest.rename(old)
    tmp.rename(dest)
    shutil.rmtree(old, ignore_errors=True)
    for name in fetched:
        log(f"catalog files: fetched image {name}")
    return {"digest": manifest.get("digest", ""), "files": len(manifest.get("files", [])), "images": fetched,
            "missing": missing}
