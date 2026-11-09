import pytest

from bsp.core.errors import InvalidRequest, NotFound
from bsp.gpio_relay.board import BoardProfile, PinKind


def test_example_40pin_profile(board):
    h = board.default_header
    assert (h.id, h.rows, h.cols, len(h.pins)) == ("J40", 20, 2, 40)
    p = board.pin(33)
    assert (p.gpio, p.kind, p.row, p.col, p.label) == (127, PinKind.GPIO, 16, 0, "GPIO127")
    assert board.pin(40).col == 1 and board.pin(40).alt == "R.GPIO[30]"
    assert board.pin(6).kind is PinKind.GND
    assert board.pin_by_gpio(118).number == 11
    assert len(list(board.gpio_pins())) == 28


def test_lookup_errors(board):
    with pytest.raises(NotFound):
        board.pin(41)
    with pytest.raises(NotFound):
        board.pin_by_gpio(5)


def test_profile_shorthand_layout_and_duplicates():
    b = BoardProfile.from_dict({"id": "x", "headers": [{"id": "H", "layout": "dual-row-split", "pin_count": 4,
                                                        "pins": {1: 10, 2: {"kind": "gnd"}, 3: 11}}]})
    assert b.pin(1).gpio == 10 and b.pin(4).kind is PinKind.NC
    assert [(b.pin(n).row, b.pin(n).col) for n in (1, 2, 3, 4)] == [(0, 0), (1, 0), (0, 1), (1, 1)]
    with pytest.raises(InvalidRequest):
        BoardProfile.from_dict({"id": "d", "headers": [{"id": "H", "pins": {1: 5, 2: 5}}]})


def test_example_26pin_profile():
    from pathlib import Path

    from bsp.gpio_relay.board import BoardCatalog

    b = BoardCatalog(Path(__file__).resolve().parents[1] / "examples" / "site" / "boards").get("example-26pin")
    assert (b.default_header.id, b.default_header.rows, len(b.default_header.pins)) == ("J26", 13, 26)
    assert [p.gpio for p in b.gpio_pins()] == [70, 71, 74, 72, 73, 91, 92, 49, 50]
    assert b.pin(8).kind is PinKind.OTHER and b.pin(8).label.startswith("UART")
