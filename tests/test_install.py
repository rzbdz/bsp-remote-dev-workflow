from pathlib import Path

from bsp.cli import install
from bsp.sdk.catalog import SdkCatalog

ROOT = Path(__file__).resolve().parents[1]
DUTS = [{"id": "board-1", "host": "lab-1", "board": "board-y", "soc": "soc-a", "sdks": ["bsp-a"]},
        {"id": "board-2", "host": "lab-2", "board": "board-x", "soc": "soc-b", "sdks": ["upstream-a"]}]


def _cat(tmp_path, paths):
    """Repo catalog with instance paths redirected into tmp dirs."""
    import shutil

    import yaml

    shutil.copytree(ROOT / "examples" / "site" / "sdk", tmp_path / "sdk")
    for kind, name, path in paths:
        p = tmp_path / "sdk" / kind / f"{name}.yaml"
        d = yaml.safe_load(p.read_text())
        d["path"] = str(path)
        p.write_text(yaml.safe_dump(d))
    return SdkCatalog(tmp_path / "sdk")


def test_single_sdk_sentence_and_backup(tmp_path):
    tree = tmp_path / "bsp-a"
    (tree / "linux").mkdir(parents=True)
    cat = _cat(tmp_path, [("instances", "bsp-a", tree)])
    (tree / "SKILL.md").write_text("old")
    res = install.install(tree / "linux", cat, DUTS, "/opt/bsp/bin/bsp")      # from a subdirectory
    text = Path(res["file"]).read_text()
    assert res["sdks"] == ["bsp-a"] and res["file"].endswith("linux/SKILL.md")
    assert "already configured as the SDK `bsp-a`" in text and "`board-1` @ lab-1" in text
    assert "board-2" not in text
    assert text.startswith("---\nname: bsp-dev-loop")                               # frontmatter kept first
    assert install.SKILL_SRC.read_text().split("---", 2)[2].strip() in text         # full skill copied
    res2 = install.install(tree / "linux", cat, DUTS, "bsp")
    assert res2["backup"] and Path(res2["backup"]).exists()


def test_shared_workspace_lists_all_targets(tmp_path):
    tree = tmp_path / "kernel-tree"
    tree.mkdir()
    cat = _cat(tmp_path, [("workspaces", "kernel-tree", tree)])
    text = Path(install.install(tree, cat, DUTS, "bsp")["file"]).read_text()
    assert "configured as these SDK targets:" in text
    assert "- `upstream-a`" in text and "- `upstream-b`" in text and "`board-2`" in text


def test_unconfigured_directory(tmp_path):
    cat = SdkCatalog(ROOT / "examples" / "site" / "sdk")
    text = Path(install.install(tmp_path, cat, None, "bsp")["file"]).read_text()
    assert "**not configured**" in text and "ask the user" in text


def test_hints_come_from_configuration(tmp_path):
    from bsp.core.site import hints

    found = hints(ROOT / "examples" / "site")
    assert ("site/ssh", (ROOT / "examples/site/hints/ssh.md").read_text().strip()) in found
    cat = SdkCatalog(ROOT / "examples" / "site" / "sdk")
    text = Path(install.install(tmp_path, cat, None, "bsp", found)["file"]).read_text()
    assert "## Hints from this installation's configuration" in text and "team's ssh key" in text
    assert text.index("team's ssh key") < text.index("Below: the full bsp skill")


def test_bspd_install_numbers_its_stages():
    """bspd-install.sh says `--> [n/N] <what>` for each stage, in order, N the same on every line."""
    import re

    script = (ROOT / "deploy" / "bspd-install.sh").read_text()
    steps = [(int(n), int(total), what) for n, total, what in re.findall(r'^\s*echo "--> \[(\d+)/(\d+)\] ([^"]+)"$', script, re.M)]
    assert [n for n, _, _ in steps] == list(range(1, len(steps) + 1)) and len(steps) >= 5
    assert {total for _, total, _ in steps} == {len(steps)}
    assert any("Python environment" in what for _, _, what in steps) and steps[-1][2] == "checking health"
    assert 'echo "BSPD-READY $ID $PORT $VERSION"' in script
    assert "--progress-bar off" in script and "install -q" not in script                # pip says what it does
