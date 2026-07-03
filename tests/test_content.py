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
    """content/show.yaml (used by the running server) must stay valid."""
    show = load_show("content/show.yaml", valid_zone_ids={"answer_a", "answer_b"})
    assert len(show.rounds) >= 1
