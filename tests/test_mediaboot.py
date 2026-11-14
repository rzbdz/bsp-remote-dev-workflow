"""Booting the media of a board whose FDL strap is held down, through steps its SoC profile declares."""
import hashlib
import re
import types

import pytest

from bsp.core.errors import HardwareTimeout, InvalidRequest
from bsp.dut.boot import BootRunner
from bsp.dut.mediaboot import MediaBoot

IMAGE = b"\x00a small image for one boot ROM\n"
SHA = hashlib.sha256(IMAGE).hexdigest()
BANNER = "ROM v2 strap=nor\r\nusb download\r\n"
STEPS = [{"do": "check", "serial": "strap=nor"}, {"do": "getvar", "name": "rom-version", "match": "^2$"},
         {"do": "stage", "image": "handoff"}, {"do": "continue"}, {"do": "wait", "serial": "SPL 20", "timeout": 5}]


def spec(strategy="strapped", **control):
    return types.SimpleNamespace(id="board-a", soc="soc-a",
                                 control={"backend": "gpio-relay", "fdl": {"strategy": strategy}, **control})


def profile(path=None, steps=STEPS, sha=SHA):
    return {"images": {"handoff": {"file": "handoff.bin", "sha256": sha, **({"path": str(path)} if path else {})}},
            "media_boot": {"name": "rom handoff", "steps": steps}}


class Board:
    """A strapped board: a reset lands in download mode; the staged image makes it boot from its media."""

    def __init__(self, banner=BANNER, rom="2", boots=True):
        self.banner, self.rom, self.boots = banner, rom, boots
        self.out, self.calls = "", []

    # fastboot
    usb, binary = "usb:1-1", "fastboot"

    def state(self):
        return {"product": "download", "usb": self.usb}

    def getvar(self, name):
        self.calls.append(f"getvar {name}")
        return self.rom

    def cmd(self, *args, timeout=0):
        self.calls.append(" ".join(a.rsplit("/", 1)[-1] for a in args))
        if args[0] == "continue" and self.boots:
            self.out += "SPL 2022.10\n"
        return "OKAY"

    # console
    alias = "s1"

    def mark(self):
        return len(self.out)

    def read_since(self, mark):
        return self.out[mark:]

    def tail(self, n):
        return self.out.splitlines()[-n:]

    def wait(self, mark, rx, fail, timeout, *rest):
        if not re.search(rx, self.out[mark:]):
            raise HardwareTimeout(f"/{rx}/ not seen")

    # control
    cfg = {"fdl": {"strategy": "strapped"}}

    def enter_fdl(self, log, seen):
        self.calls.append("reset")
        self.out += self.banner

    def reset(self, log):
        self.calls.append("plain reset")


def runner(board, media=None):
    return BootRunner("board-a", board, board, board, store=None, download_mode={"fastboot": "download"}, media=media)


def image(tmp_path, data=IMAGE):
    p = tmp_path / "handoff.bin"
    p.write_bytes(data)
    return p


def test_an_already_waiting_download_port_uses_its_existing_rom_evidence_without_a_reset(tmp_path):
    # The person (or an earlier relay reset) already left this strapped board in FDL. No new banner arrives now:
    # judging only the console text since this request would falsely reject the valid NOR download state.
    b = Board()
    b.out = BANNER
    lines = []
    r = MediaBoot(spec(), profile(image(tmp_path))).run(runner(b), lines.append)
    assert b.calls == ["getvar rom-version", "stage handoff.bin", "continue"]
    assert r["name"] == "rom handoff" and any("already present" in line for line in lines)


def test_a_human_strapped_board_with_an_existing_banner_does_not_need_another_request(tmp_path):
    # Human/relay is only how the action is performed; it must not change the media-boot decision.
    s = spec()
    s.control["backend"] = "human"
    b = Board()
    b.out = BANNER
    MediaBoot(s, profile(image(tmp_path))).run(runner(b), lambda _line: None)
    assert b.calls == ["getvar rom-version", "stage handoff.bin", "continue"]


def test_a_strapped_board_boots_its_media_through_the_steps_its_soc_declares(tmp_path):
    b = Board()
    m = MediaBoot(spec(), profile(image(tmp_path)))
    assert m.available() and m.name == "rom handoff"
    lines = []
    r = m.run(runner(b), lines.append)
    assert b.calls == ["reset", "getvar rom-version", "stage handoff.bin", "continue"]
    assert r["name"] == "rom handoff" and any("done" in l for l in lines)


@pytest.mark.parametrize("board,error", [
    (Board(banner="ROM v2 strap=sd\r\nusb download\r\n"), "does not match /strap=nor/"),
    (Board(rom="3"), "rom-version = '3' does not match"),
])
def test_a_board_the_steps_do_not_suit_is_refused_before_anything_is_staged(tmp_path, board, error):
    with pytest.raises(InvalidRequest, match=error):
        MediaBoot(spec(), profile(image(tmp_path))).run(runner(board), lambda l: None)
    assert not any(c.startswith("stage") for c in board.calls)


def test_an_image_that_does_not_hash_right_is_never_staged_and_a_silent_board_is_reported(tmp_path):
    b = Board()
    with pytest.raises(InvalidRequest, match="has sha256 .*, not "):
        MediaBoot(spec(), profile(image(tmp_path, b"something else"))).run(runner(b), lambda l: None)
    assert b.calls == []
    stuck = Board(boots=False)
    with pytest.raises(HardwareTimeout, match="SPL 20"):
        MediaBoot(spec(), profile(image(tmp_path))).run(runner(stuck), lambda l: None)


def test_who_needs_it_and_who_can_have_it(tmp_path):
    assert "not needed" in MediaBoot(spec("button"), profile(tmp_path)).unavailable()
    assert "switched off" in MediaBoot(spec(media_boot=False), profile(tmp_path)).unavailable()
    assert "declares no media_boot" in MediaBoot(spec(), {}).unavailable()
    assert "bsp images sync" in MediaBoot(spec(), profile()).unavailable()
    assert "may not use: flash" in MediaBoot(spec(), profile(tmp_path, steps=[{"do": "flash"}])).unavailable()
    # One DUT can bring its own steps, whatever its SoC profile says.
    own = MediaBoot(spec(media_boot={"name": "own", "steps": [{"do": "continue"}]}), {})
    assert own.available() and own.name == "own"


def test_recipes_cold_boot_the_flashed_media_of_a_strapped_board_instead_of_skipping_it(tmp_path):
    steps = [{"do": "reset", "when": "fdl-not-strapped", "why": "cold boot the flashed media"},
             {"do": "wait", "when": "fdl-not-strapped", "serial": "SPL 20", "timeout": 5}]
    b, lines = Board(), []
    runner(b, MediaBoot(spec(), profile(image(tmp_path)))).run_steps(steps, lines.append)
    assert b.calls[-2:] == ["stage handoff.bin", "continue"] and "plain reset" not in b.calls
    assert not any("skipped" in l for l in lines)
    # Without a way to boot its media, the steps are skipped and the log says why.
    b2, lines2 = Board(), []
    runner(b2, MediaBoot(spec(), {})).run_steps(steps, lines2.append)
    assert sum("skipped" in l and "declares no media_boot" in l for l in lines2) == 2 and b2.calls == []
