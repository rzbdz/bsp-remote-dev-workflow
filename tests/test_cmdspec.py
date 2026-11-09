import pytest

from bsp.core.cmdspec import CapturingParser, CliExit, CommandSpec, Param, build_parser, parse_tokens
from bsp.core.commands import Command, CommandRegistry
from bsp.core.errors import InvalidRequest

SPEC = CommandSpec(
    "gpio.pulse",
    "pulse",
    [
        Param("target"),
        Param("level", required=False, default="low", choices=["high", "low"]),
        Param("ms", "integer", required=False, default=100),
        Param("wait", "boolean", required=False, default=True),
    ],
)


def test_validate_coerces_query_strings():
    assert SPEC.validate({"target": "P3", "ms": "250", "wait": "false"}) == {
        "target": "P3", "level": "low", "ms": 250, "wait": False,
    }


@pytest.mark.parametrize("args", [{}, {"target": "x", "ms": "abc"}, {"target": "x", "level": "mid"}, {"target": "x", "bogus": 1}])
def test_validate_rejects(args):
    with pytest.raises(InvalidRequest):
        SPEC.validate(args)


def test_spec_routes_and_roundtrip():
    assert SPEC.http_path == "/api/v1/gpio/{target}/pulse"
    assert SPEC.tool_name == "gpio_pulse"
    assert SPEC.cli_usage == "bsp gpio pulse <target> [--level] [--ms] [--wait]"
    again = CommandSpec.from_dict(SPEC.to_dict())
    assert again.input_schema() == SPEC.input_schema()


def test_parser_from_specs():
    p = build_parser([SPEC], parser_class=CapturingParser)
    assert parse_tokens(p, ["gpio", "pulse", "P3", "--ms", "5", "--no-wait"]) == ("gpio.pulse", {"target": "P3", "ms": 5, "wait": False})
    with pytest.raises(CliExit) as e:
        parse_tokens(p, ["gpio", "pulse"])
    assert e.value.status == 2
    with pytest.raises(CliExit) as e:
        parse_tokens(p, ["gpio"])
    assert e.value.status == 0 and "pulse" in e.value.message


def test_run_line():
    reg = CommandRegistry()
    reg.add(Command(SPEC, lambda **kw: kw, render=lambda r: f"pulsed {r['target']} {r['ms']}"))
    assert reg.run_line("bsp gpio pulse P3 --ms 7")[:2] == (0, "pulsed P3 7\n")
    code, out, _ = reg.run_line("help gpio pulse")
    assert code == 0 and "--ms" in out
    assert reg.run_line("gpio pulse 'unterminated")[0] == 2
    assert reg.get("gpio_pulse") is reg.get("gpio.pulse")
