from server.models import AudienceDetail, AudienceSummary, Snapshot, ZoneMap


def test_audience_summary_parses_change_event_shape():
    s = AudienceSummary.model_validate(
        {
            "gid": 5,
            "visible": True,
            "center": [10, 20],
            "bbox": [0, 0, 20, 40],
            "floor": [0.1, 0.2],
            "floor_valid": True,
            "zone": "answer_a",
        }
    )
    assert s.gid == 5
    assert s.zone == "answer_a"


def test_audience_summary_defaults_for_invisible_gid():
    s = AudienceSummary.model_validate({"gid": 5, "visible": False})
    assert s.center is None
    assert s.zone is None


def test_snapshot_parses_detail_entries():
    snap = Snapshot.model_validate(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "active_people": 1,
            "zone_counts": {"answer_a": 1},
            "people": [
                {
                    "gid": 1,
                    "visible": True,
                    "center": [1, 2],
                    "bbox": [0, 0, 2, 4],
                    "floor": [0.1, 0.1],
                    "floor_valid": True,
                    "zone": "answer_a",
                    "confidence": 0.8,
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "last_seen": "2026-01-01T00:00:00+00:00",
                    "duration_seen_seconds": 3.5,
                }
            ],
        }
    )
    assert isinstance(snap.people[0], AudienceDetail)
    assert snap.people[0].duration_seen_seconds == 3.5


def test_zone_map_zone_ids_includes_default():
    zm = ZoneMap.model_validate(
        {
            "enabled": True,
            "default_zone": "outside",
            "zones": [{"id": "answer_a", "label": "A", "points": [[0, 0], [1, 0], [1, 1]]}],
        }
    )
    assert zm.zone_ids() == {"answer_a", "outside"}
