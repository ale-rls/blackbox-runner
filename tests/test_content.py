from __future__ import annotations

import pytest

from server import content_db
from server.content import ContentError, load_show, validate_show
from server.pocketbase_client import PocketBaseClient

VALID_SHOW = {
    "version": "1",
    "rounds": [
        {
            "id": "r1",
            "question": "Coffee or tea?",
            "type": "majority",
            "options": [{"zone": "a", "label": "Coffee"}, {"zone": "b", "label": "Tea"}],
        }
    ],
}


def _write(tmp_path, data):
    import yaml

    path = tmp_path / "show.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_load_valid_show(tmp_path):
    path = _write(tmp_path, VALID_SHOW)
    show = load_show(path, valid_zone_ids={"a", "b"})
    assert len(show.rounds) == 1
    assert show.rounds[0].id == "r1"


def test_rejects_unknown_zone(tmp_path):
    path = _write(tmp_path, VALID_SHOW)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids={"a", "c"})  # "b" is missing


def test_rejects_duplicate_round_ids(tmp_path):
    data = {
        "version": "1",
        "rounds": [dict(VALID_SHOW["rounds"][0]), dict(VALID_SHOW["rounds"][0])],
    }
    path = _write(tmp_path, data)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids={"a", "b"})


def test_rejects_duplicate_zones_within_round(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "options": [{"zone": "a", "label": "X"}, {"zone": "a", "label": "Y"}],
            }
        ],
    }
    path = _write(tmp_path, data)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids={"a"})


def test_correct_zone_requires_exactly_one_correct_option(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "type": "correct_zone",
                "options": [{"zone": "a", "label": "X"}, {"zone": "b", "label": "Y"}],
            }
        ],
    }
    path = _write(tmp_path, data)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids={"a", "b"})


def test_correct_zone_with_one_correct_option_loads(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "type": "correct_zone",
                "options": [
                    {"zone": "a", "label": "X", "correct": True},
                    {"zone": "b", "label": "Y"},
                ],
            }
        ],
    }
    path = _write(tmp_path, data)
    show = load_show(path, valid_zone_ids={"a", "b"})
    assert show.rounds[0].options[0].correct is True


def test_repo_show_yaml_loads_against_its_own_zones():
    """content/show.yaml (the authoring copy) must stay valid against the
    dev TrackingBox zone set — the two move in lockstep."""
    import json

    dev_config = json.loads(open("dev/trackingbox.config.json").read())
    zone_ids = {z["id"] for z in dev_config["zones"]["zones"]}
    show = load_show("content/show.yaml", valid_zone_ids=zone_ids)
    assert len(show.rounds) >= 1


@pytest.mark.asyncio
async def test_repo_show_yaml_imports_into_db_and_loads_back(pb, fake_pocketbase):
    """End to end: the real authoring copy imported via the import script
    (pointed at the fake PocketBase), then loaded from the DB the way the
    server does at startup."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "scripts/import_content.py",
            "--content",
            "content/show.yaml",
            "--pb-url",
            fake_pocketbase.url,
            "--pb-email",
            "test@example.com",
            "--pb-password",
            "pw",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    file_show = load_show("content/show.yaml")
    db_show = await content_db.load_show_db(pb)
    assert db_show.version == file_show.version
    assert [r.id for r in db_show.rounds] == [r.id for r in file_show.rounds]
    assert db_show == file_show


@pytest.mark.asyncio
async def test_import_script_rejects_invalid_yaml_without_writing(pb, fake_pocketbase, tmp_path):
    import subprocess
    import sys

    bad = tmp_path / "show.yaml"
    bad.write_text(
        "version: '1'\n"
        "rounds:\n"
        "  - id: r1\n"
        "    question: '?'\n"
        "    options:\n"
        "      - {zone: a, label: only one}\n"  # < 2 options: invalid
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/import_content.py",
            "--content",
            str(bad),
            "--pb-url",
            fake_pocketbase.url,
            "--pb-email",
            "test@example.com",
            "--pb-password",
            "pw",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "INVALID" in result.stderr
    # Nothing was written: the store holds no show.
    assert await pb.load_content() == ("", [])


# ---------------------------------------------------------------------- #
# DB-backed loading (content_db) — counterparts to the file-based tests
# ---------------------------------------------------------------------- #


async def _db_with(pb: PocketBaseClient, raw: dict) -> PocketBaseClient:
    await pb.save_content(str(raw.get("version", "1")), content_db.rows_from_raw(raw["rounds"]))
    return pb


@pytest.mark.asyncio
async def test_load_show_db_round_trips_all_fields(pb):
    raw = {
        "version": "3",
        "rounds": [
            {
                "id": "intro",
                "question": "Intro",
                "type": "narration",
                "duration_s": 0,
                "grace_s": 0,
                "points": 0,
                "text": "Hallo.\nWillkommen.",
                "audio": "intro.mp3",
            },
            {
                "id": "r1",
                "question": "?",
                "form": "cross",
                "zone_layout": "x_axis",
                "form_labels": {"x_left": "l", "x_right": "r", "y_top": "t", "y_bottom": "b"},
                "options": [
                    {"zone": "x_left", "label": "L"},
                    {"zone": "x_right", "label": "R", "correct": True},
                ],
            },
        ],
    }
    await _db_with(pb, raw)
    show = await content_db.load_show_db(pb)
    assert show == validate_show(raw)
    assert show.rounds[0].text == "Hallo.\nWillkommen."
    assert show.rounds[1].zone_layout == "x_axis"
    assert show.rounds[1].options[1].correct is True


@pytest.mark.asyncio
async def test_load_show_db_empty_db_is_valid_empty_show(pb):
    show = await content_db.load_show_db(pb)
    assert show.rounds == []


@pytest.mark.asyncio
async def test_load_show_db_rejects_unknown_zone(pb):
    await _db_with(pb, VALID_SHOW)
    with pytest.raises(ContentError):
        await content_db.load_show_db(pb, valid_zone_ids={"a", "c"})  # "b" is missing
    assert (await content_db.load_show_db(pb, valid_zone_ids={"a", "b"})).rounds[0].id == "r1"


@pytest.mark.asyncio
async def test_derived_zone_layout_not_persisted_as_explicit(pb):
    # A scale round derives x_axis at validation time; the stored row must
    # keep zone_layout NULL so a later form change re-derives the layout.
    raw = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "form": "scale",
                "form_labels": {"left": "l", "right": "r"},
                "options": [{"zone": "s_l", "label": "L"}, {"zone": "s_r", "label": "R"}],
            }
        ],
    }
    await _db_with(pb, raw)
    _, rows = await pb.load_content()
    assert rows[0].zone_layout is None
    assert (await content_db.load_show_db(pb)).rounds[0].zone_layout == "x_axis"


def test_narration_round_loads_without_options(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "intro",
                "question": "Intro",
                "type": "narration",
                "duration_s": 0,
                "text": "Hallo, herzlich willkommen.",
                "audio": "intro.mp3",
            }
        ],
    }
    path = _write(tmp_path, data)
    show = load_show(path, valid_zone_ids=set())
    round_ = show.rounds[0]
    assert round_.options == []
    assert round_.text == "Hallo, herzlich willkommen."
    assert round_.audio == "intro.mp3"


def test_narration_round_rejects_options(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "intro",
                "question": "Intro",
                "type": "narration",
                "options": [{"zone": "a", "label": "X"}, {"zone": "b", "label": "Y"}],
            }
        ],
    }
    path = _write(tmp_path, data)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids={"a", "b"})


def test_question_round_still_requires_two_options(tmp_path):
    data = {
        "version": "1",
        "rounds": [{"id": "r1", "question": "?", "options": [{"zone": "a", "label": "X"}]}],
    }
    path = _write(tmp_path, data)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids={"a"})


def test_form_requires_its_labels(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "form": "scale",
                "form_labels": {"left": "kalt"},  # "right" missing
                "options": [{"zone": "a", "label": "X"}, {"zone": "b", "label": "Y"}],
            }
        ],
    }
    path = _write(tmp_path, data)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids={"a", "b"})


def test_form_with_labels_round_trips(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "form": "cross",
                "form_labels": {
                    "x_left": "getrennt",
                    "x_right": "verschmolzen",
                    "y_top": "ich führe",
                    "y_bottom": "die Maschine führt",
                },
                "text": "Positioniere dich im Feld.",
                "options": [
                    {"zone": "tl", "label": "A"},
                    {"zone": "tr", "label": "B"},
                    {"zone": "bl", "label": "C"},
                    {"zone": "br", "label": "D"},
                ],
            }
        ],
    }
    path = _write(tmp_path, data)
    round_ = load_show(path, valid_zone_ids={"a", "b"}).rounds[0]
    assert round_.form == "cross"
    assert round_.form_labels["y_top"] == "ich führe"
    assert round_.text == "Positioniere dich im Feld."


def test_zone_layout_derived_from_form(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "form": "rings",
                "form_labels": {"center": "ja", "edge": "nein"},
                "options": [
                    {"zone": "ring_center", "label": "ja"},
                    {"zone": "ring_mid", "label": "teils"},
                    {"zone": "ring_outer", "label": "nein"},
                ],
            }
        ],
    }
    path = _write(tmp_path, data)
    round_ = load_show(path, valid_zone_ids=set()).rounds[0]
    assert round_.zone_layout == "circles"


def test_explicit_zone_layout_overrides_form_default(tmp_path):
    # A cross question judged on one axis only: two options, x_axis layout.
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "form": "cross",
                "zone_layout": "x_axis",
                "form_labels": {"x_left": "l", "x_right": "r", "y_top": "t", "y_bottom": "b"},
                "options": [{"zone": "x_left", "label": "L"}, {"zone": "x_right", "label": "R"}],
            }
        ],
    }
    path = _write(tmp_path, data)
    assert load_show(path, valid_zone_ids=set()).rounds[0].zone_layout == "x_axis"


def test_quadrants_layout_requires_exactly_four_options(tmp_path):
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "form": "quadrants",
                "options": [{"zone": "tl", "label": "A"}, {"zone": "tr", "label": "B"}],
            }
        ],
    }
    path = _write(tmp_path, data)
    with pytest.raises(ContentError):
        load_show(path, valid_zone_ids=set())


def test_layout_round_zones_need_not_exist_in_tracking_map(tmp_path):
    # Layout rounds use logical, per-question zone names resolved from floor
    # positions — only "choice" rounds validate against TrackingBox zones.
    data = {
        "version": "1",
        "rounds": [
            {
                "id": "r1",
                "question": "?",
                "form": "scale",
                "form_labels": {"left": "l", "right": "r"},
                "options": [{"zone": "scale_left", "label": "L"}, {"zone": "scale_right", "label": "R"}],
            }
        ],
    }
    path = _write(tmp_path, data)
    show = load_show(path, valid_zone_ids={"ritual"})
    assert show.rounds[0].zone_layout == "x_axis"


def test_choice_round_has_no_layout_and_validates_zones(tmp_path):
    path = _write(tmp_path, VALID_SHOW)
    show = load_show(path, valid_zone_ids={"a", "b"})
    assert show.rounds[0].zone_layout is None
