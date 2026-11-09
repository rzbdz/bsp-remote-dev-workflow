import threading

from bsp.gpio_relay.pinmux import PinmuxInspector, parse_gpio_ranges, parse_pinmux_pins

# sample debugfs pinmux output (format from a real kernel; names neutralised)
RANGES = """GPIO ranges handled:
0: gpio-ctl.bank0 GPIOS [0 - 31] PINS [0 - 31]
0: gpio-ctl.bank2 GPIOS [64 - 95] PINS [64 - 95]
0: gpio-ctl.bank3 GPIOS [96 - 127] PINS [96 - 127]
"""
PINS = """pin 83 (GPIO_83): i2c-ctl-3 (GPIO UNCLAIMED) function i2c3-cfg group i2c3-cfg.i2c3-0-pins
pin 124 (GPIO_124): (MUX UNCLAIMED) (GPIO UNCLAIMED)
pin 127 (GPIO_127): (MUX UNCLAIMED) gpio-ctl.bank3:127
pin 0 (GPIO_00): i2c-ctl-0 (GPIO UNCLAIMED) function i2c0-cfg group i2c0-cfg.i2c0-0-pins
"""


def test_parse():
    m = parse_pinmux_pins(PINS, parse_gpio_ranges(RANGES))
    assert m[83]["conflict"] and m[83]["function"] == "i2c3-cfg"
    assert not m[124]["conflict"] and m[124]["mux_owner"] is None
    assert not m[127]["conflict"] and m[127]["gpio_owner"] == "gpio-ctl.bank3:127"
    assert m[0]["conflict"]


def test_k1_pin_numbers_differ_from_gpio_numbers():
    # sample where pin numbers differ from GPIO numbers
    ranges = """GPIO ranges handled:
70: gpio-ctl GPIOS [70 - 73] PINS [71 - 74]
90: gpio-ctl GPIOS [90 - 92] PINS [127 - 129]
"""
    pins = """pin 70 (PIN70): uart-ctl-0 (GPIO UNCLAIMED) function uart0_grp group uart0_grp
pin 71 (PIN71): (MUX UNCLAIMED) (GPIO UNCLAIMED)
pin 128 (PIN128): (MUX UNCLAIMED) (GPIO UNCLAIMED)
"""
    m = parse_pinmux_pins(pins, parse_gpio_ranges(ranges))
    assert set(m) == {70, 91}                 # pin 71 -> GPIO70, pin 128 -> GPIO91
    assert m[70]["pad"] == "PIN71" and not m[70]["conflict"]   # pin 70 (uart) has no GPIO -> dropped


def test_inspector_reads_dir(tmp_path):
    (tmp_path / "gpio-ranges").write_text(RANGES)
    (tmp_path / "pinmux-pins").write_text(PINS)
    ins = PinmuxInspector(str(tmp_path))
    assert ins.refresh()["state"] == "ok"
    assert ins.get(83)["conflict"]


def test_inspector_single_flight_when_kernel_blocks(tmp_path, monkeypatch):
    gate = threading.Event()
    ins = PinmuxInspector(str(tmp_path), timeout=0.05)
    monkeypatch.setattr(ins, "_read", lambda box: gate.wait(5))
    assert ins.refresh()["state"] == "unavailable"          # timed out
    s = ins.refresh()                                        # first read still stuck -> no second thread
    assert s["state"] == "unavailable" and "still blocked" in s["error"]
    gate.set()


def test_disabled():
    assert PinmuxInspector(None).refresh()["state"] == "disabled"
