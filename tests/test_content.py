from __future__ import annotations

import pytest

from server.content import ContentError, load_show

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
    """content/show.yaml (used by the running server) must stay valid
    against the dev TrackingBox zone set — the two move in lockstep."""
    import json

    dev_config = json.loads(open("dev/trackingbox.config.json").read())
    zone_ids = {z["id"] for z in dev_config["zones"]["zones"]}
    show = load_show("content/show.yaml", valid_zone_ids=zone_ids)
    assert len(show.rounds) >= 1


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
