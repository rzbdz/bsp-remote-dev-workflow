"""Site images: fetched once by hash into the site, and found there."""
import hashlib
import subprocess

from bsp.dut import images
from bsp.dut.socs import SocProfiles

HELPER = b"\x00helper for one boot ROM\n"
SHA = hashlib.sha256(HELPER).hexdigest()


def test_images_are_fetched_once_by_hash_into_the_site_and_found_there(tmp_path):
    site = tmp_path / "site"
    (site / "socs" / "k3").mkdir(parents=True)
    repo = tmp_path / "kit"
    (repo / "boot").mkdir(parents=True)
    (repo / "boot" / "helper.bin").write_bytes(HELPER)
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "kit"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    (site / "socs" / "k3" / "soc.yaml").write_text(f"""
soc: k3
images:
  helper: {{file: helper.bin, sha256: {SHA}, source: {{git: {repo}, ref: HEAD, path: boot/helper.bin}}}}
  wrong: {{file: other.bin, sha256: {"0" * 64}, source: {{file: {repo / "boot" / "helper.bin"}}}}}
  handmade: {{file: probe.bin}}
""")
    profiles = SocProfiles([site / "socs"])
    assert {r["image"]: r["state"] for r in images.plan(profiles, site)} == {"handmade": "missing", "helper": "fetchable", "wrong": "fetchable"}
    r = images.sync(profiles, site, cache=tmp_path / "cache")
    assert r["fetched"] == ["k3/helper"] and [f["image"] for f in r["failed"]] == ["k3/wrong"]
    assert (site / ".blobs" / SHA / "helper.bin").read_bytes() == HELPER
    assert not (site / ".blobs" / ("0" * 64) / "other.bin").exists()       # nothing kept that hashes wrong
    again = SocProfiles([site / "socs"])
    assert again.get("k3")["images"]["helper"]["path"] == str(site / ".blobs" / SHA / "helper.bin")
    assert images.sync(again, site, cache=tmp_path / "cache")["present"] == ["k3/helper"]
