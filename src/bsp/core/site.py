"""Layers of configuration knowledge. Each layer may contribute the same kinds of
data (boards/, socs/, hooks/, sdk/{families,servers,workspaces,instances}/, duts/,
hints/), merged in order; later layers override earlier ones:

  core   the bsp repo itself: mechanism and abstract concepts only, no vendor knowledge
  packs  packs/<name>/ in the repo: public hardware knowledge, like drivers — SoC
         profiles (boot-ROM fingerprints are fixed in silicon), pin maps of boards you
         can buy, hooks for public tool formats. Selected by the site (`packs:` in
         <site>/bsp.yaml; default: every pack in the repo). A future "distribution" is
         a curated set of packs plus procedure templates.
  site   private, outside the repo: hosts, build servers, SDK trees and layouts, boot /
         flash recipes, probe images, DUTs, credentials, hints. Found at $BSP_SITE,
         ~/.config/bsp/site or /etc/bsp/site.

examples/site/ in the repo is a complete, made-up site showing every format.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PACKS = REPO / "packs"


def site_dir() -> Path | None:
    cands = [os.environ.get("BSP_SITE"), str(Path.home() / ".config" / "bsp" / "site"), "/etc/bsp/site"]
    for c in cands:
        if c and Path(c).expanduser().is_dir():
            return Path(c).expanduser().resolve()
    return None


def site_settings(site: Path | None) -> dict:
    p = site / "bsp.yaml" if site else None
    if p and p.exists():
        import yaml

        return yaml.safe_load(p.read_text()) or {}
    return {}


def packs(site: Path | None = None) -> list[Path]:
    """Pack dirs selected by the site (all packs in the repo when the site does not say)."""
    available = {p.name: p for p in sorted(PACKS.iterdir()) if p.is_dir()} if PACKS.is_dir() else {}
    wanted = site_settings(site).get("packs")
    if wanted is None:
        return list(available.values())
    missing = [w for w in wanted if w not in available]
    if missing:
        from .errors import InvalidRequest

        raise InvalidRequest(f"site selects unknown pack(s) {missing}", hint=f"available: {', '.join(available)}")
    return [available[w] for w in wanted]


def layers(site: Path | None = None, use_env: bool = True) -> list[Path]:
    """Layer roots, lowest priority first: selected packs, then the site."""
    if site is None and use_env:
        site = site_dir()
    return packs(site) + ([site] if site else [])


def layered(kind: str, site: Path | None = None) -> list[Path]:
    """Existing `kind` dirs (e.g. "sdk", "boards", "socs", "hooks") across all layers, lowest first."""
    return [d for d in (root / kind for root in layers(site)) if d.is_dir()]


def hints(site: Path | None = None) -> list[tuple[str, str]]:
    """(layer/name, text) of every <layer>/hints/*.md: packs first (by pack name), then "site/..."."""
    if site is None:
        site = site_dir()
    out = []
    for root in layers(site, use_env=False):
        label = "site" if site and root == site else root.name
        d = root / "hints"
        if d.is_dir():
            out += [(f"{label}/{p.stem}", p.read_text(encoding="utf-8").strip()) for p in sorted(d.glob("*.md"))]
    return out
