from __future__ import annotations

import pytest

from server.zones import LAYOUTS, OPTION_COUNTS, resolve_zone


def test_every_layout_has_option_bounds():
    assert set(LAYOUTS) == set(OPTION_COUNTS)


@pytest.mark.parametrize(
    ("x", "expected"),
    [(0.0, "left"), (0.49, "left"), (0.5, "right"), (1.0, "right")],
)
def test_x_axis_two_bands(x, expected):
    assert resolve_zone("x_axis", ["left", "right"], x, 0.5) == expected


def test_x_axis_three_bands():
    zones = ["left", "mid", "right"]
    assert resolve_zone("x_axis", zones, 0.1, 0.9) == "left"
    assert resolve_zone("x_axis", zones, 0.5, 0.1) == "mid"
    assert resolve_zone("x_axis", zones, 0.9, 0.5) == "right"


@pytest.mark.parametrize(
    ("y", "expected"),
    [(0.0, "top"), (0.49, "top"), (0.5, "bottom"), (1.0, "bottom")],
)
def test_y_axis_two_bands(y, expected):
    assert resolve_zone("y_axis", ["top", "bottom"], 0.5, y) == expected


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [(0.2, 0.2, "tl"), (0.8, 0.2, "tr"), (0.2, 0.8, "bl"), (0.8, 0.8, "br")],
)
def test_quadrants(x, y, expected):
    assert resolve_zone("quadrants", ["tl", "tr", "bl", "br"], x, y) == expected


def test_circles_rings_from_center_to_edge():
    zones = ["center", "mid", "outer"]
    assert resolve_zone("circles", zones, 0.5, 0.5) == "center"
    assert resolve_zone("circles", zones, 0.5, 0.55) == "center"  # d = 0.1
    assert resolve_zone("circles", zones, 0.5, 0.75) == "mid"  # d = 0.5
    assert resolve_zone("circles", zones, 0.5, 0.95) == "outer"  # d = 0.9
    assert resolve_zone("circles", zones, 0.5, 1.0) == "outer"  # on the edge


def test_circles_corners_count_as_outermost():
    # The floor corners lie beyond the outer painted circle (d > 1).
    assert resolve_zone("circles", ["center", "mid", "outer"], 0.0, 0.0) == "outer"


def test_out_of_range_positions_clamp_to_edge_bands():
    assert resolve_zone("x_axis", ["left", "right"], -0.2, 0.5) == "left"
    assert resolve_zone("x_axis", ["left", "right"], 1.3, 0.5) == "right"
