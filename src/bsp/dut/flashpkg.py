"""Flash packages: what `dut flash` writes to a board, as one file in the host's artifact cache.

A package is a .tar.gz / .tgz / .tar / .zip (the CLI sends a local directory as a .tar). It reaches the cache
either uploaded (PUT /api/v1/artifacts/<sha256>) or downloaded by the host from an http(s) URL (`fetch`). It is
unpacked into a fresh directory (`unpack`) that never reaches outside itself: absolute names, `..`, links and
special files are refused. An archive holding one top-level directory unpacks to that directory.
"""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ..core.errors import HardwareError, InvalidRequest
from .hostio import SHA256, ArtifactStore

Log = Callable[[str], None]

GiB = 1 << 30
DEFAULT_MAX_BYTES = 16 * GiB


def check_url(url: str) -> str:
    """An http(s) URL a person gave: nothing else, no spaces or control characters, a host."""
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise InvalidRequest("not a usable package URL", hint="an http:// or https:// URL without spaces")
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise InvalidRequest(f"package URL scheme {u.scheme or '(none)'!r} is not allowed",
                             hint="use http:// or https://, or give a local path (the CLI uploads it)")
    if not u.hostname:
        raise InvalidRequest(f"package URL has no host: {url}")
    return url


def is_url(ref: str) -> bool:
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", ref or ""))


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to http(s) (urllib would also follow ftp)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _name_from(url: str, headers) -> str:
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    name = m.group(1) if m else PurePosixPath(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    return name or "package"


def fetch(store: ArtifactStore, url: str, sha256: str | None, log: Log, max_bytes: int = DEFAULT_MAX_BYTES,
          timeout: float = 60) -> dict[str, Any]:
    """Download `url` into the artifact cache (once: a cached `sha256` is not fetched again). With `sha256` the
    download must hash to it; without, it is kept under the hash it has. Refuses more than `max_bytes`."""
    check_url(url)
    want = (sha256 or "").lower() or None
    if want and not SHA256.fullmatch(want):
        raise InvalidRequest(f"not a sha256: {sha256!r}", hint="64 hex digits")
    if want and store.find(want):
        p = store.find(want)
        log(f"package {p.name} already on this host (sha256 {want[:12]})")
        return {"sha256": want, "path": str(p), "size": p.stat().st_size, "fetched": False}
    opener = urllib.request.build_opener(_HttpOnlyRedirects())
    req = urllib.request.Request(url, headers={"User-Agent": "bsp-dut-flash"})
    t0 = time.monotonic()
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise HardwareError(f"downloading the package failed: HTTP {e.code} {e.reason}",
                            hint="check the URL from this board host (it downloads the package itself)") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise HardwareError(f"downloading the package failed: {reason}",
                            hint="the board host downloads the package itself: is the URL reachable from it?") from None
    with resp:
        length = resp.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        if total is not None and total > max_bytes:
            raise InvalidRequest(f"package is {total:,} B, more than this host takes ({max_bytes:,} B)",
                                 hint="raise modules.dut.flash.max_package_bytes in the host config")
        import hashlib

        h, size, next_log = hashlib.sha256(), 0, 0.0
        incoming = store.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / f"{os.getpid()}.{time.monotonic_ns()}.part"
        name = _name_from(resp.geturl(), resp.headers)
        log(f"downloading {name}" + (f" ({total:,} B)" if total else "") + f" from {urllib.parse.urlsplit(url).hostname}")
        try:
            with open(tmp, "wb") as f:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    size += len(chunk)
                    if size > max_bytes:
                        raise InvalidRequest(f"package is more than this host takes ({max_bytes:,} B)",
                                             hint="raise modules.dut.flash.max_package_bytes in the host config")
                    h.update(chunk)
                    f.write(chunk)
                    if time.monotonic() >= next_log:
                        next_log = time.monotonic() + 10
                        if size:
                            log(f"  {size:,} B" + (f" of {total:,} ({size * 100 // total}%)" if total else ""))
            got = h.hexdigest()
            if total is not None and size != total:
                raise HardwareError(f"download cut short: {size:,} of {total:,} B", hint="try again")
            if want and got != want:
                raise InvalidRequest(f"downloaded package hashes to {got}, not {want}",
                                     hint="the file at the URL is not the one meant (or --sha256 is wrong)")
            if not size:
                raise InvalidRequest("the package URL gave an empty file")
            final = store.adopt(tmp, got, name)
        finally:
            tmp.unlink(missing_ok=True)
    log(f"downloaded {name} ({size:,} B in {time.monotonic() - t0:.1f}s, sha256 {got[:12]})")
    return {"sha256": got, "path": str(final), "size": size, "fetched": True}


# ------------------------------------------------------------------ unpack --
def _safe_name(name: str) -> PurePosixPath:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or re.match(r"^[A-Za-z]:", name) or ".." in p.parts:
        raise InvalidRequest(f"package entry {name!r} points outside the package", hint="the archive is not usable")
    return p


def kind_of(path: Path) -> str:
    if path.is_dir():
        return "dir"
    if zipfile.is_zipfile(path):
        return "zip"
    if tarfile.is_tarfile(path):
        return "tar"
    raise InvalidRequest(f"{path.name} is not a package", hint="a .tar.gz, .tgz, .tar or .zip (or a directory)")


def unpack(path: Path, dest: Path, max_bytes: int = 4 * DEFAULT_MAX_BYTES) -> Path:
    """Unpack the archive at `path` into the empty directory `dest`; returns the package directory (`dest`, or its
    one top-level directory). Refuses anything that would land outside `dest`, links, special files, and more than
    `max_bytes` unpacked."""
    dest.mkdir(parents=True, exist_ok=True)
    kind = kind_of(path)
    total = 0
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            for i in infos:
                _safe_name(i.filename)
                if (i.external_attr >> 16) & 0o170000 == 0o120000:
                    raise InvalidRequest(f"package entry {i.filename!r} is a link", hint="the archive is not usable")
                total += i.file_size
                if total > max_bytes:
                    raise InvalidRequest(f"package unpacks to more than {max_bytes:,} B")
            for i in infos:
                rel = _safe_name(i.filename)
                target = dest.joinpath(*rel.parts)
                if i.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
    else:
        with tarfile.open(path, "r:*") as t:
            for m in t:
                rel = _safe_name(m.name)
                if m.issym() or m.islnk():
                    raise InvalidRequest(f"package entry {m.name!r} is a link", hint="the archive is not usable")
                if not (m.isfile() or m.isdir()):
                    raise InvalidRequest(f"package entry {m.name!r} is not a file or directory",
                                         hint="the archive is not usable")
                if not rel.parts:
                    continue
                target = dest.joinpath(*rel.parts)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += m.size
                if total > max_bytes:
                    raise InvalidRequest(f"package unpacks to more than {max_bytes:,} B")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = t.extractfile(m)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
    # nothing may have landed outside dest (a name that resolved through a directory planted earlier, say)
    root = dest.resolve()
    for p in dest.rglob("*"):
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            raise InvalidRequest(f"package entry {p.relative_to(dest)} points outside the package")
    return top_dir(dest)


def top_dir(d: Path) -> Path:
    """The package directory: `d`, or the one directory it holds when that is all it holds."""
    entries = [p for p in d.iterdir() if not p.name.startswith(".") and p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return d
