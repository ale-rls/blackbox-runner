"""Per-question floor layouts: resolve answer zones from floor positions.

Each question uses the *whole* floor, divided by its own shape — instead of
one static TrackingBox zone map that carves every shape into the floor at
once. A player's answer zone is computed here from their normalized floor
position ``(x, y)`` in ``[0, 1]²`` and the round's option list:

- ``x_axis``: equal vertical bands, options ordered left -> right.
- ``y_axis``: equal horizontal bands, options ordered top -> bottom.
- ``quadrants``: exactly 4 options, ordered top-left, top-right,
  bottom-left, bottom-right.
- ``circles``: equal-width concentric rings around the floor center,
  options ordered center -> edge; positions beyond the outermost ring
  (the floor corners) count as the outermost option.

Future shapes are added by registering a resolver in ``LAYOUTS`` (and its
option-count bounds in ``OPTION_COUNTS``), plus a matching drawing in the
admin floor map.
"""

from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

# resolver(x, y, n_options) -> option index
Resolver = Callable[[float, float, int], int]


def _band(value: float, n: int) -> int:
    """Which of ``n`` equal bands ``value`` falls in, clamped so 1.0 (and
    out-of-range positions) land in the last band instead of out of range."""
    return min(max(int(value * n), 0), n - 1)


def _x_axis(x: float, y: float, n: int) -> int:
    return _band(x, n)


def _y_axis(x: float, y: float, n: int) -> int:
    return _band(y, n)


def _quadrants(x: float, y: float, n: int) -> int:
    return (0 if y < 0.5 else 2) + (0 if x < 0.5 else 1)


def _circles(x: float, y: float, n: int) -> int:
    # Distance from the floor center, normalized so 1.0 is the inscribed
    # circle (touching the floor edges, as painted on stage).
    return _band(math.hypot(x - 0.5, y - 0.5) / 0.5, n)


LAYOUTS: dict[str, Resolver] = {
    "x_axis": _x_axis,
    "y_axis": _y_axis,
    "quadrants": _quadrants,
    "circles": _circles,
}

# (min, max) options a layout supports; max None = unbounded.
OPTION_COUNTS: dict[str, tuple[int, Optional[int]]] = {
    "x_axis": (2, None),
    "y_axis": (2, None),
    "quadrants": (4, 4),
    "circles": (2, None),
}


def resolve_zone(layout: str, zones: Sequence[str], x: float, y: float) -> str:
    """Zone id (from ``zones``, in layout order) for floor position (x, y)."""
    return zones[LAYOUTS[layout](x, y, len(zones))]
