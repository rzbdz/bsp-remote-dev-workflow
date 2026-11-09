from pathlib import Path

from bsp.dut.sshlogin import ATTACH, MAP, SSHD, SUDOERS, ConsoleLogins, login_name


class FakeSystem:
    """Records commands and keeps /etc/passwd and /etc/group under a temp root."""

    def __init__(self, root: Path):
        self.root, self.calls = root, []
        (root / "etc").mkdir(parents=True)
        (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\nalice:x:1000:1000:Alice:/home/alice:/bin/bash\n")
        (root / "etc/group").write_text("root:x:0:\n")

    def __call__(self, argv):
        self.calls.append(argv)
        pw, gr = self.root / "etc/passwd", self.root / "etc/group"
        if argv[0] == "useradd":
            user, comment = argv[-1], argv[argv.index("--comment") + 1]
            pw.write_text(pw.read_text() + f"{user}:x:999:999:{comment}:/:/usr/local/lib/bsp/console-shell\n")
        elif argv[0] == "userdel":
            pw.write_text("".join(l + "\n" for l in pw.read_text().splitlines() if not l.startswith(argv[1] + ":")))
        elif argv[0] == "usermod":
            lines = [l.split(":") for l in pw.read_text().splitlines()]
            for l in lines:
                if l[0] == argv[-1]:
                    l[4] = argv[2]
            pw.write_text("".join(":".join(l) + "\n" for l in lines))
        elif argv[0] == "groupadd":
            gr.write_text(gr.read_text() + f"{argv[-1]}:x:998:\n")
        elif argv[0] == "groupdel":
            gr.write_text("".join(l + "\n" for l in gr.read_text().splitlines() if not l.startswith(argv[1] + ":")))


def _logins(tmp_path, enabled=True):
    sysm = FakeSystem(tmp_path)
    return ConsoleLogins({"enabled": enabled}, "lab-1", 8765, "/opt/bsp/venv/bin/smtty", run=sysm, root=tmp_path), sysm


def test_login_names():
    assert login_name("bsp-", "k1-Board_2") == "bsp-k1-board_2"
    assert login_name("bsp-", "x" * 40) is None


def test_sync_adds_renames_and_cleans_up(tmp_path):
    lg, sysm = _logins(tmp_path)
    r = lg.sync({"board-1": "board-1", "board-2": "con-2"})
    assert r["added"] == ["bsp-board-1", "bsp-board-2"] and ["passwd", "-d", "bsp-board-1"] in sysm.calls
    conf = (tmp_path / SSHD.relative_to("/")).read_text()
    assert "Match User bsp-*" in conf and "PermitEmptyPasswords yes" in conf and conf.rstrip().endswith("Match all")
    assert '"bsp-board-2"' in (tmp_path / MAP.relative_to("/")).read_text()
    assert (tmp_path / SUDOERS.relative_to("/")).read_text().endswith(f'{ATTACH} ""\n')
    assert any(c[0] == "visudo" for c in sysm.calls) and ["sshd", "-t"] in sysm.calls
    # board-2 removed, board-3 added; alice is never touched
    r = lg.sync({"board-1": "board-1", "board-3": "board-3"})
    assert r["removed"] == ["bsp-board-2"] and r["added"] == ["bsp-board-3"]
    assert "alice:" in (tmp_path / "etc/passwd").read_text()
    # feature off: every trace goes away
    off = ConsoleLogins({"enabled": False}, "lab-1", 8765, "smtty", run=sysm, root=tmp_path)
    r = off.sync({"board-1": "board-1"})
    assert sorted(r["removed"]) == ["bsp-board-1", "bsp-board-3"] and not r["logins"]
    for p in (SSHD, SUDOERS, ATTACH, MAP):
        assert not (tmp_path / p.relative_to("/")).exists()
    assert "bsp-console" not in (tmp_path / "etc/group").read_text()


def test_unchanged_sync_is_quiet(tmp_path):
    lg, sysm = _logins(tmp_path)
    lg.sync({"board-1": "board-1"})
    n = len(sysm.calls)
    r = lg.sync({"board-1": "board-1"})
    assert len(sysm.calls) == n and not (r["added"] or r["removed"] or r["config_changed"])
