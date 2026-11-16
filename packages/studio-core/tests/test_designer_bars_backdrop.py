from studio_core.production.designer import bar

BOX = {"x": 100, "y": 200, "width": 880, "height": 1400}


def picture(edge_width: int) -> bytearray:
    """Black for edge_width px from the window's left edge, then a varied picture."""
    raw = bytearray(1080 * 1920 * 3)
    for y in range(BOX["y"], BOX["y"] + BOX["height"]):
        for x in range(BOX["x"] + edge_width, BOX["x"] + 200):
            value = 40 + (y * 7 + x * 3) % 60
            i = 3 * (y * 1080 + x)
            raw[i : i + 3] = bytes((value, value, value))
    return raw


def test_wide_black_padding_is_still_a_bar():
    assert bar(bytes(picture(60)), BOX, BOX["x"] + 6, 80) == "black"


def test_thin_black_backdrop_at_the_edge_is_not_a_bar():
    assert bar(bytes(picture(12)), BOX, BOX["x"] + 6, 80) is None
