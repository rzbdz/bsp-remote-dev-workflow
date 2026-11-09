"""Booting a board from its own media when its reset always lands in download mode.

A board whose FDL (download) strap is held down can be loaded from RAM and flashed, but a plain
reset never boots what is on its media. Some SoCs have a way around that, done from download mode.
bsp does not know any such way itself: a SoC profile (or one DUT's `control`) declares it as steps,
the same steps SDK boot recipes use:

    media_boot:
      name: <label shown in `bsp dut status`>
      steps:                                   # run with the board reset into download mode first
        - {do: check, serial: <regex>}         # the boot ROM's banner says this is a board the steps suit
        - {do: getvar, name: <var>, match: <regex>}
        - {do: stage, image: <key in images>}  # an image from the SoC profile, verified by sha256
        - {do: continue}
        - {do: hook, kind: boot, name: <hook>} # or anything a hook does (hooks/README.md)
        - {do: wait, serial: <regex>, timeout: 60}

A step that fails (`check`, `getvar` with `match`) stops the boot before anything else is staged.
Nothing in the steps is specific to bsp: they live in a pack or, for anything not public, in the
site (or the registry's catalog), with any images they stage.

A board with an FDL line (`fdl.strategy: button`) boots its media with a plain reset and needs none
of this; a DUT can opt out with `control.media_boot: false`.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable

from ..core.errors import InvalidRequest

Log = Callable[[str], None]
# Steps a media boot may use: no SDK artifacts, no flashing.
STEPS = ("check", "getvar", "stage", "continue", "oem", "wait", "settle", "hook")


class MediaBoot:
    def __init__(self, spec, profile: dict[str, Any]):
        self.spec, self.profile = spec, profile
        own = spec.control.get("media_boot")
        self.cfg: dict[str, Any] = own if isinstance(own, dict) else (profile.get("media_boot") or {})

    @property
    def name(self) -> str | None:
        return self.cfg.get("name") or ("media boot" if self.cfg else None)

    def unavailable(self) -> str | None:
        """Why this board cannot boot its media this way (None: it can)."""
        strategy = (self.spec.control.get("fdl") or {}).get("strategy", "strapped")
        if strategy != "strapped":
            return "not needed: a plain reset boots its media (FDL is not strapped)"
        if self.spec.control.get("media_boot") is False:
            return "switched off for this DUT (control.media_boot: false)"
        steps = self.cfg.get("steps")
        if not steps:
            return f"the {self.spec.soc or 'unknown'} SoC profile declares no media_boot steps"
        bad = [s.get("do") for s in steps if s.get("do") not in STEPS]
        if bad:
            return f"media_boot has steps it may not use: {', '.join(map(str, bad))} (allowed: {', '.join(STEPS)})"
        images = self.profile.get("images") or {}
        for s in steps:
            if "image" in s:
                img = images.get(s["image"])
                if not img:
                    return f"media_boot stages image {s['image']!r}, which the SoC profile does not list"
                if not img.get("path"):
                    return f"image {s['image']!r} ({img.get('file')}) is not on this host (`bsp images sync` where the site is, then deploy)"
        return None

    def available(self) -> bool:
        return self.unavailable() is None

    def images(self) -> dict[str, Any]:
        """The images the steps stage, each checked against its sha256 first."""
        out = {}
        for key in {s["image"] for s in self.cfg.get("steps", []) if "image" in s}:
            img = self.profile["images"][key]
            if not img.get("sha256"):
                raise InvalidRequest(f"image {key!r} has no sha256 in the SoC profile", hint=f"pin it: images.{key}.sha256")
            with open(img["path"], "rb") as f:
                actual = hashlib.sha256(f.read()).hexdigest()
            if actual != img["sha256"].lower():
                raise InvalidRequest(f"image {img['path']} has sha256 {actual}, not {img['sha256']}",
                                     hint="run `bsp images sync` again; never stage an unverified image")
            out[key] = img
        return out

    def run(self, runner, log: Log) -> dict[str, Any]:
        """Reset the board into download mode and run the steps (runner: the board's BootRunner)."""
        why = self.unavailable()
        if why:
            raise InvalidRequest(f"{self.spec.id} cannot boot its media: {why}")
        images = self.images()
        t0 = time.monotonic()
        log(f"{self.name}: reset into download mode")
        since = runner.console.mark()
        runner.enter_fdl(log)
        runner.run_steps(self.cfg["steps"], log, images=images, what=self.name, since=since)
        log(f"{self.name}: done in {time.monotonic() - t0:.1f}s")
        return {"dut": self.spec.id, "name": self.name, "elapsed_s": round(time.monotonic() - t0, 1)}
