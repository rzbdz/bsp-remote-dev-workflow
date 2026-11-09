from pathlib import Path

import pytest

from bsp.core.errors import InvalidRequest, NotFound
from bsp.sdk.catalog import SdkCatalog, sharing

ROOT = Path(__file__).resolve().parents[1] / "examples" / "site" / "sdk"


@pytest.fixture(scope="module")
def cat():
    return SdkCatalog(ROOT)


def test_catalogue(cat):
    assert cat.instances.names() == ["bsp-a", "bsp-b", "upstream-a", "upstream-b"]
    boot = {i: s.summary()["boot"] for i, s in cat.instances.items()}
    assert boot["bsp-a"] == {"load": "ready", "download": "ready"}
    assert boot["upstream-a"] == {"load": "ready", "download": "todo"}
    assert boot["bsp-b"] == {"load": "todo", "download": "todo"}
    assert cat.get("bsp-b").server.ssh == "dev@192.0.2.11"


def test_build_commands(cat):
    assert cat.get("bsp-a").build_command("uboot") == "cd /home/dev/work/bsp-a && make uboot"
    assert cat.get("bsp-a").build_command("kernel", clean=True).endswith("make clean_kernel")
    b = cat.get("bsp-b").build_command("linux")
    assert b.startswith("bash -c ") and "envsetup.sh evb" in b and b.rstrip("'").endswith("build-linux")
    assert cat.get("upstream-a").build_command("kernel").endswith("./build.sh -b board-x")
    with pytest.raises(InvalidRequest) as e:
        cat.get("bsp-a").build_command("kernal")
    assert "kernel" in e.value.hint


def test_artifacts_by_role(cat):
    assert cat.get("bsp-a").artifacts()["kernel-fit"] == "/home/dev/work/bsp-a/output/Image.itb"
    assert cat.get("bsp-b").artifacts()["dtb"].endswith("out/evb/board_evb.dtb")
    assert cat.get("upstream-b").artifacts()["kernel-fit"].endswith("kernel-tree/staging-b/Image.itb")


def test_boot_plans(cat):
    full = cat.get("bsp-a").boot_plan("load")
    assert [s["do"] for s in full["steps"]][-2:] == ["continue", "wait"]
    assert full["steps"][1]["file"].endswith("/output/FSBL.bin")
    short = cat.get("bsp-a").boot_plan("load", "uboot-only")
    assert not any(s.get("artifact") == "kernel-fit" for s in short["steps"])
    flash = cat.get("bsp-a").boot_plan("download")
    assert [s["do"] for s in flash["steps"]].count("hook") == 1
    with pytest.raises(InvalidRequest) as e:
        cat.get("bsp-b").boot_plan("download")
    assert "no flash procedure" in e.value.hint                  # todo placeholder explains itself
    with pytest.raises(InvalidRequest):
        cat.get("bsp-a").boot_plan("ram")


def test_unknown_sdk_suggests(cat):
    with pytest.raises(NotFound) as e:
        cat.get("bspa")
    assert "bsp-a" in e.value.hint


def test_instance_must_declare_both_methods(tmp_path):
    import shutil

    shutil.copytree(ROOT, tmp_path / "sdk")
    p = tmp_path / "sdk" / "instances" / "bsp-a.yaml"
    p.write_text(p.read_text().split("  download:")[0])
    with pytest.raises(InvalidRequest) as e:
        SdkCatalog(tmp_path / "sdk")
    assert "download" in e.value.message


def test_workspaces_soc_and_layers(cat, tmp_path):
    a, b = cat.get("upstream-a"), cat.get("upstream-b")
    assert a.workspace is b.workspace and a.path == b.path              # one tree, one lease
    assert sharing(cat, "kernel-tree") == ["upstream-a", "upstream-b"]
    assert (a.soc, b.soc, cat.get("bsp-a").soc) == ("soc-b", "soc-a", "soc-a")
    assert a.dev_projects == ["."] and cat.get("bsp-a").workspace.id == "bsp-a"   # implicit workspace
    assert a.path_prepend == ["~/toolchains/riscv/bin"]                  # build-server env applies
    # a later layer overrides an earlier one (site over pack)
    over = tmp_path / "over" / "instances"
    over.mkdir(parents=True)
    (over / "bsp-a.yaml").write_text((ROOT / "instances" / "bsp-a.yaml").read_text().replace("soc: soc-a", "soc: soc-z"))
    assert SdkCatalog([ROOT, tmp_path / "over"]).get("bsp-a").soc == "soc-z"
