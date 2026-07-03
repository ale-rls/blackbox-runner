"""Pydantic models mirroring TrackingBox's REST/WebSocket payload shapes.

These are a deliberate copy, not an import — TrackingBox is a versioned,
read-only dependency (see README for the pinned commit) and this module is
the single place that would need updating if its wire format ever changes.

Shapes mirrored from ``audience_tracker.models.AudienceState`` (``summary()``
and ``detail()``) and ``audience_tracker.zones.ZoneMap`` (``as_dict()``).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

Point = tuple[float, float]
BBox = tuple[float, float, float, float]


class AudienceSummary(BaseModel):
    """Shape of a WS change event, and each entry TrackingBox's /api/audience."""

    gid: int
    visible: bool
    center: Optional[tuple[int, int]] = None
    bbox: Optional[tuple[int, int, int, int]] = None
    floor: Optional[Point] = None
    floor_valid: bool = False
    zone: Optional[str] = None


class AudienceDetail(AudienceSummary):
    """Shape of each entry in a snapshot's ``people`` list."""

    confidence: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    duration_seen_seconds: float = 0.0


class Snapshot(BaseModel):
    """Shape of GET /api/snapshot and the WS ``{"type": "snapshot", ...}`` payload."""

    timestamp: str
    active_people: int
    zone_counts: dict[str, int] = {}
    people: list[AudienceDetail] = []


class ZoneDefinition(BaseModel):
    id: str
    label: str
    points: list[Point]


class ZoneMap(BaseModel):
    """Shape of GET /api/zones."""

    enabled: bool
    default_zone: Optional[str] = None
    zones: list[ZoneDefinition] = []

    def zone_ids(self) -> set[str]:
        ids = {z.id for z in self.zones}
        if self.default_zone:
            ids.add(self.default_zone)
        return ids
