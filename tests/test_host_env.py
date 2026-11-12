"""Host environment, hook request extras (env/network/product_dir), and their defaults."""

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bsp.core.env import expand, resolve_refs
from bsp.core.errors import InvalidRequest
from bsp.core.hooks import HookRunner
from bsp.dut.boot import BootRunner
from bsp.dut.module import resolve_network
from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "examples" / "site" / "hosts" / "dev-mock.yaml"


# ------------------------------------------------------------------- config --
def test_env_refs_are_resolved_recursively_and_unset_names_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("BSP_TEST_SMTTY", "/opt/serial/bin/smtty")
    cfg = tmp_path / "host.yaml"
    cfg.write_text(
        "host: {id: h1}\n"
        "server: {port: 8765}\n"
        "env:\n"
        "  BSP_SMTTY: env:BSP_TEST_SMTTY      # a value taken from bsp-hostd's environment\n"
        "  BSP_SERIAL_LOG_DIR: /var/log/serial\n"
        "modules:\n"
        "  dut:\n"
        "    smtty: env:BSP_TEST_SMTTY\n"
        "    paths: [env:BSP_TEST_SMTTY, literal, {nested: env:BSP_TEST_SMTTY}]\n",
        encoding="utf-8",
    )
    h = HostConfig.load(cfg)
    assert h.env == {"BSP_SMTTY": "/opt/serial/bin/smtty", "BSP_SERIAL_LOG_DIR": "/var/log/serial"}
    assert h.modules["dut"]["smtty"] == "/opt/serial/bin/smtty"        # recursive, any depth
    assert h.modules["dut"]["paths"] == ["/opt/serial/bin/smtty", "literal",
                                         {"nested": "/opt/serial/bin/smtty"}]

    cfg.write_text("host: {id: h1}\nmodules:\n  dut:\n    smtty: env:BSP_TEST_NOT_SET\n", encoding="utf-8")
    with pytest.raises(InvalidRequest) as e:
        HostConfig.load(cfg)
    assert "BSP_TEST_NOT_SET" in e.value.message and "not set" in e.value.message
    assert "hostd.env" in e.value.hint


def test_only_a_whole_string_is_an_env_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("BSP_TEST_X", "value")
    assert resolve_refs("env:BSP_TEST_X") == "value"
    assert resolve_refs("prefix env:BSP_TEST_X") == "prefix env:BSP_TEST_X"
    assert resolve_refs({"a": ["env:BSP_TEST_X"]}) == {"a": ["value"]}


def test_the_env_map_must_be_a_mapping(tmp_path):
    cfg = tmp_path / "host.yaml"
    cfg.write_text("host: {id: h1}\nenv: [BSP_X]\n", encoding="utf-8")
    with pytest.raises(InvalidRequest):
        HostConfig.load(cfg)


# ------------------------------------------------------------------ expand --
def test_hook_args_expand_with_shell_defaults():
    env = {"BSP_TFTP_SERVER": "192.0.2.20", "EMPTY": ""}
    assert expand("${BSP_TFTP_SERVER}/image.itb", env) == "192.0.2.20/image.itb"
    assert expand("${MISSING}", env) == ""
    assert expand("${MISSING:-fallback}", env) == "fallback"
    assert expand("${EMPTY:-fallback}", env) == "fallback"          # like the shell: unset or empty
    assert expand({"a": ["${BSP_TFTP_SERVER}", "${NOPE:-x}"]}, env) == {"a": ["192.0.2.20", "x"]}
    assert expand({"n": 3, "b": True}, env) == {"n": 3, "b": True}


def test_a_hook_gets_the_host_env_in_its_process_and_its_request(tmp_path):
    hook = tmp_path / "probe" / "env"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/usr/bin/env python3\nimport json, os, sys\nreq = json.load(sys.stdin)\n"
                    "print(json.dumps({'process': os.environ.get('BSP_TFTP_SERVER'), 'request': req['env']}))\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    runner = HookRunner((str(tmp_path),), env={"BSP_TFTP_SERVER": "198.51.100.7"})
    out = runner.run("probe", "env", {"env": runner.env}).data
    assert out == {"process": "198.51.100.7", "request": {"BSP_TFTP_SERVER": "198.51.100.7"}}


# -------------------------------------------------------- boot hook request --
class _Console:
    alias = "board1"

    def mark(self):
        return (Path("/dev/null"), 0)


class _Fastboot:
    usb, binary = "usb:2-1.2", "fastboot"


def _capture_hook(tmp_path: Path) -> HookRunner:
    hook = tmp_path / "flash" / "capture"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/usr/bin/env python3\nimport json, os, sys\nreq = json.load(sys.stdin)\n"
                    "open(os.environ['CAPTURE'], 'w').write(json.dumps(req))\n"
                    "print(json.dumps({'ok': True}))\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    return HookRunner((str(tmp_path),))


def test_a_boot_hook_request_has_env_network_sdk_artifacts_and_product_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTURE", str(tmp_path / "req.json"))
    runner = BootRunner("b1", None, _Fastboot(), _Console(), None, hooks=_capture_hook(tmp_path),
                        env={"BSP_TFTP_SERVER": "192.0.2.20"},
                        network={"board_subnet": "192.0.2.0/24", "host_address": "192.0.2.1",
                                 "export_address": "203.0.113.9"},
                        product_dir=lambda sdk, artifacts: f"/mnt/products/{sdk}")
    runner.run_steps([{"do": "hook", "kind": "flash", "name": "capture",
                       "args": {"tftp": "${BSP_TFTP_SERVER}/img.itb", "d": "${NOPE:-none}"}}],
                     lambda m: None, sdk="bsp-a", artifacts_dir="/home/dev/work/bsp-a/output")
    req = json.loads((tmp_path / "req.json").read_text())
    assert req["sdk"] == "bsp-a" and req["artifacts_dir"] == "/home/dev/work/bsp-a/output"
    assert req["product_dir"] == "/mnt/products/bsp-a"
    assert req["env"] == {"BSP_TFTP_SERVER": "192.0.2.20"}
    assert req["network"] == {"board_subnet": "192.0.2.0/24", "host_address": "192.0.2.1",
                              "export_address": "203.0.113.9", "console": "board1"}
    assert req["args"] == {"tftp": "192.0.2.20/img.itb", "d": "none"}


def test_without_a_product_dir_the_artifact_directory_is_the_product(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTURE", str(tmp_path / "req.json"))
    runner = BootRunner("b1", None, _Fastboot(), _Console(), None, hooks=_capture_hook(tmp_path))
    runner.run_steps([{"do": "hook", "kind": "flash", "name": "capture"}], lambda m: None,
                     sdk="bsp-a", artifacts_dir="/home/dev/work/bsp-a/output")
    req = json.loads((tmp_path / "req.json").read_text())
    assert req["product_dir"] == "/home/dev/work/bsp-a/output"
    assert req["network"] == {"console": "board1"}                 # network is optional


# ------------------------------------------------ host module: config keys --
@pytest.fixture
def dut(tmp_path, monkeypatch):
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    with TestClient(create_app(HostConfig.load(MOCK))) as c:
        yield c.app.state.hub.modules["dut"]


def test_product_dir_is_a_literal_or_a_template(dut):
    assert dut.product_dir("bsp-a", "/home/dev/work/bsp-a/output") == "/home/dev/work/bsp-a/output"
    dut.product_dir_tpl = "/srv/products/{sdk}"
    assert dut.product_dir("bsp-a", "/remote/output") == "/srv/products/bsp-a"
    dut.product_dir_tpl = "/srv/{sdk}/{artifacts_dir}"
    assert dut.product_dir("bsp-a", "out") == "/srv/bsp-a/out"
    dut.product_dir_tpl = "{artifacts_dir}"
    with pytest.raises(InvalidRequest) as e:
        dut.product_dir(None, None)
    assert "artifacts_dir" in e.value.message
    dut.product_dir_tpl = "{nope}"
    with pytest.raises(InvalidRequest) as e:
        dut.product_dir("bsp-a", "out")
    assert "{sdk}" in e.value.hint and "{artifacts_dir}" in e.value.hint


def test_network_defaults_from_announce_and_bind():
    explicit = resolve_network({"board_subnet": "10.0.0.0/24", "host_address": "10.0.0.2",
                                "export_address": "203.0.113.5"}, local=[])
    assert explicit == {"board_subnet": "10.0.0.0/24", "host_address": "10.0.0.2",
                        "export_address": "203.0.113.5"}
    # an announced address is what other machines are told to use
    announced = resolve_network({}, bind="0.0.0.0", announce={"addresses": ["198.51.100.4"]}, local=[])
    assert announced == {"board_subnet": None, "host_address": "198.51.100.4",
                         "export_address": "198.51.100.4"}
    # a concrete bind is this host's address; 0.0.0.0 and loopback fall back to the machine's own
    assert resolve_network({}, bind="192.168.7.2", local=[])["host_address"] == "192.168.7.2"
    assert resolve_network({}, bind="127.0.0.1", local=["10.1.2.3"])["host_address"] == "10.1.2.3"
    # no DHCP knowledge in bsp: board_subnet stays unset unless configured
    assert resolve_network({}, bind="127.0.0.1", local=["10.1.2.3"])["board_subnet"] is None
