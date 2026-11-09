import time

import pytest

from bsp.core.errors import Conflict, Forbidden, HardwareTimeout, InvalidRequest, NotFound
from bsp.gpio_relay.backends.guarded import GuardedBackend
from bsp.gpio_relay.backends.mock import MockBackend
from bsp.gpio_relay.backends.sysfs import SysfsBackend
from bsp.gpio_relay.pinmux import PinmuxInspector


@pytest.mark.parametrize("ref", [33, "33", "P33", "p33", "pin33", "pin:33", "J40:33", "GPIO127", "gpio:127"])
def test_resolve_refs(make_service, ref):
    assert make_service().resolve(ref).key == "J40:33"


def test_resolve_alias_and_diagnostics(make_service):
    svc = make_service(aliases={"DUT3/reset": "P11", "DUT3/fdl": "GPIO62"})
    assert svc.resolve("dut3/RESET").number == 11
    assert svc.resolve("/DUT3/fdl/").number == 13
    with pytest.raises(NotFound) as e:
        svc.resolve("DUT3/rest")                       # typo -> did you mean
    assert "DUT3/reset" in e.value.details["did_you_mean"]
    with pytest.raises(NotFound) as e:
        svc.resolve("DUT3")                            # prefix -> list children
    assert set(e.value.details["children"]) == {"DUT3/reset", "DUT3/fdl"} and "prefix" in e.value.hint
    with pytest.raises(InvalidRequest) as e:
        svc.channel("P6")
    assert "P5" in e.value.hint                        # suggests nearby GPIO pins


def test_gpio_mode(make_service, backend):
    svc = make_service()
    assert svc.get("P33")["state"] == "unclaimed"
    assert svc.set_level("P33", "low")["line"] == {"claimed": True, "direction": "out", "value": 0, "owner": "bsp"}
    assert svc.set_level("P33", "input")["state"] == "input"
    with pytest.raises(InvalidRequest):
        svc.switch("P33", "on")
    with pytest.raises(InvalidRequest):
        svc.channel("P6")  # GND


def test_button_is_open_drain_active_low(make_service, backend):
    svc = make_service(baseline={"P11": {"mode": "button"}}, aliases={"rst": "P11"})
    assert svc.switch("rst", "press")["state"] == "pressed"
    assert backend.inner.status(118).direction == "out" and backend.inner.status(118).value == 0
    assert svc.switch("rst", "release")["state"] == "released"
    assert backend.inner.status(118).direction == "in"   # hi-Z, never driven high
    with pytest.raises(InvalidRequest) as e:
        svc.set_level("rst", "high")
    assert e.value.details["mode"] == "button" and "press" in e.value.details["actions"]


def test_relay_polarity_and_toggle(make_service, backend):
    svc = make_service(baseline={"P13": {"mode": "relay", "active_low": True}})
    assert svc.switch("P13", "on")["line"]["value"] == 0
    assert svc.switch("P13", "toggle")["state"] == "off"
    assert backend.inner.status(62).value == 1          # push-pull drives inactive high
    assert svc.switch("P13", "toggle")["state"] == "on"


def test_pulse_restores(make_service, backend):
    svc = make_service(baseline={"P11": {"mode": "button"}})
    svc.switch("P11", "release")
    backend.inner.history.clear()
    snap = svc.pulse("P11", 5)
    assert snap["state"] == "released"
    assert [h[1:] for h in backend.inner.history] == [("out", 0), ("in", None)]
    svc.set_level("P33", "high")
    svc.pulse("P33", 5, "low")
    assert svc.get("P33")["state"] == "high"


def test_async_pulse_marks_busy(make_service, monkeypatch):
    svc = make_service(baseline={"P11": {"mode": "button"}})
    monkeypatch.setattr(svc, "LOCK_WAIT_S", 0.02)
    snap = svc.pulse("P11", 200, wait=False)
    assert snap["pulse_started"] and snap["busy"]
    with pytest.raises(Conflict):
        svc.switch("P11", "press")
    time.sleep(0.35)
    assert svc.get("P11")["busy"] is None


def test_pulse_bounds(make_service):
    with pytest.raises(InvalidRequest):
        make_service(max_pulse_ms=1000).pulse("P33", 5000)


def test_config_overlay_persist_and_reset(make_service, tmp_path):
    svc = make_service(baseline={"P11": {"mode": "button"}})
    snap = svc.configure("P11", {"mode": "relay", "active_low": True})
    assert snap["channel"]["mode"] == "relay" and snap["origin"] == "runtime"
    assert "channels" in (tmp_path / "channels.json").read_text()
    svc2 = make_service(baseline={"P11": {"mode": "button"}})   # a fresh service sees the overlay
    assert svc2.get("P11")["channel"]["active_low"] is True
    assert svc2.export_config()["channels"] == {"P11": {"mode": "relay", "active_low": True}}
    assert svc2.configure("P11", {}, reset=True)["channel"]["mode"] == "button"
    with pytest.raises(InvalidRequest) as e:
        svc2.configure("P11", {"name": "x"})
    assert "alias" in e.value.hint


def test_config_switch_to_button_releases_and_clears_mode_opts(make_service, backend):
    svc = make_service(baseline={"P13": {"mode": "relay", "active_low": True, "drive": "push_pull"}})
    svc.switch("P13", "on")
    snap = svc.configure("P13", {"mode": "button"})
    assert snap["channel"]["drive"] == "open_drain" and snap["state"] == "released"


def test_alias_lifecycle(make_service, tmp_path):
    svc = make_service(aliases={"LAB/fixed": "P7"})
    a = svc.alias_add("DUT3/reset", "GPIO123", tags="dut3, reset", owner="dut-manager")
    assert (a["pin"], a["tags"], a["owner"]) == ("P32", ["dut3", "reset"], "dut-manager")
    assert svc.alias_add("DUT3/reset", "P32")["pin"] == "P32"            # idempotent
    with pytest.raises(Conflict):
        svc.alias_add("dut3/RESET", "P11")                               # case-insensitive, needs replace
    assert svc.alias_add("DUT3/reset", "P11", tags="dut3", replace=True)["pin"] == "P11"
    svc.alias_add("DUT3/fdl", "DUT3/reset")                              # alias of alias is flattened
    assert svc.get("P11")["aliases"] == ["DUT3/fdl", "DUT3/reset"]
    assert [x["name"] for x in svc.alias_list(prefix="DUT3")] == ["DUT3/fdl", "DUT3/reset"]
    assert [x["name"] for x in svc.alias_list(tag="dut3")] == ["DUT3/reset"]
    assert svc.alias_resolve("DUT3/fdl")["pin"]["target"] == "P11"
    # persisted; host-config aliases are read-only
    svc2 = make_service(aliases={"LAB/fixed": "P7"})
    assert svc2.resolve("DUT3/fdl").number == 11
    with pytest.raises(Forbidden):
        svc2.alias_delete("LAB/fixed")
    with pytest.raises(Forbidden):
        svc2.alias_add("LAB/fixed", "P8", replace=True)
    assert {x["name"] for x in svc2.alias_delete("DUT3", prefix=True)} == {"DUT3/fdl", "DUT3/reset"}
    assert svc2.export_config()["aliases"] == {"LAB/fixed": "P7"}     # merged view, ready to commit


@pytest.mark.parametrize("name", ["P12", "33", "gpio5", "J40:3", "bad name", "a//b", ""])
def test_alias_name_rules(make_service, name):
    with pytest.raises(InvalidRequest):
        make_service().alias_add(name, "P11")


def test_alias_target_must_be_gpio(make_service):
    with pytest.raises(InvalidRequest):
        make_service().alias_add("DUT1/gnd", "P6")


def test_protected(make_service):
    svc = make_service(protected=["P29"])
    with pytest.raises(Forbidden):
        svc.set_level("GPIO122", "low")
    assert svc.get("P29")["protected"]


def test_pinmux_conflict_needs_force(make_service, tmp_path):
    d = tmp_path / "pc"
    d.mkdir()
    (d / "gpio-ranges").write_text("0: bank2 GPIOS [64 - 95] PINS [64 - 95]\n")
    (d / "pinmux-pins").write_text("pin 83 (GPIO_83): f0614000.i2c (GPIO UNCLAIMED) function i2c3-2-cfg group g\n")
    svc = make_service(pinmux=PinmuxInspector(str(d)))
    assert svc.refresh_pinmux()["conflicts"].keys() == {"P3"}
    with pytest.raises(Conflict):
        svc.set_level("P3", "low")
    assert svc.set_level("P3", "low", force=True)["state"] == "low"


def test_initial_applied_on_startup(make_service, backend):
    svc = make_service(baseline={"P13": {"mode": "relay", "active_low": True, "initial": "off"}, "P11": {"mode": "button"}})
    svc.startup()
    assert backend.inner.status(62).value == 1      # relay off (active low)
    assert backend.inner.status(118).direction == "in"   # button default initial = released


def test_events_published(make_service):
    svc = make_service()
    svc.set_level("P33", "high")
    ev = svc.bus.recent(1)[0]
    assert ev.topic == "gpio.changed" and ev.data["target"] == "P33" and ev.data["state"] == "high"


class _Hang(MockBackend):
    def set_output(self, gpio, value):
        time.sleep(1)


def test_guarded_backend_times_out():
    g = GuardedBackend(_Hang(), timeout=0.05)
    with pytest.raises(HardwareTimeout):
        g.set_output(1, 1)
    assert g.info()["degraded"] and g.info()["hung_calls"] == 1


def test_sysfs_backend_on_fake_tree(tmp_path):
    (tmp_path / "export").write_text("")
    (tmp_path / "unexport").write_text("")
    g = tmp_path / "gpio127"   # pre-exported by someone else
    g.mkdir()
    for f, v in (("direction", "in"), ("value", "1"), ("active_low", "0")):
        (g / f).write_text(v)
    be = SysfsBackend(root=str(tmp_path))
    st = be.status(127)
    assert (st.claimed, st.direction, st.value, st.owner) == (True, "in", 1, "sysfs")
    be.set_output(127, 0)
    assert (g / "direction").read_text() == "low"
    be.free(127)
    assert (g / "direction").read_text() == "in"
    assert (tmp_path / "unexport").read_text() == ""   # foreign export left alone
    assert be.status(5).claimed is False
