"""show_store: DB-backed round edits that validate the whole merged show
before writing, so an invalid edit leaves the stored rows untouched."""

from __future__ import annotations

import pytest
import yaml

from server import content_db
from server.content import ContentError
from server.persistence import Database
from server.show_store import update_round

SHOW = """\
version: "2"
rounds:
  - id: intro
    question: "Intro"
    type: narration
    duration_s: 0
    grace_s: 0
    points: 0
    text: |
      Hallo.
      Willkommen.

  - id: q1
    question: "Coffee or tea?"
    type: majority
    duration_s: 45
    grace_s: 5
    points: 0
    form: scale
    form_labels:
      left: "Coffee"
      right: "Tea"
    options:
      - zone: answer_a
        label: "Coffee"
      - zone: answer_b
        label: "Tea"

  - id: q2
    question: "Pick a corner"
    type: majority
    duration_s: 45
    grace_s: 5
    points: 0
    options:
      - zone: answer_a
        label: "A"
      - zone: answer_b
        label: "B"
"""


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "game.db"))
    raw = yaml.safe_load(SHOW)
    database.save_content(raw["version"], content_db.rows_from_raw(raw["rounds"]))
    try:
        yield database
    finally:
        database.close()


def test_update_question_preserves_other_rounds(db):
    show = update_round(db, "q1", {"question": "Beer or wine?"})

    assert [r.id for r in show.rounds] == ["intro", "q1", "q2"]
    assert show.rounds[1].question == "Beer or wine?"
    assert show.rounds[0].text == "Hallo.\nWillkommen.\n"

    # The edit persisted: a fresh load from the DB sees the same show.
    reloaded = content_db.load_show_db(db)
    assert reloaded.version == "2"
    assert reloaded.rounds[1].question == "Beer or wine?"
    assert reloaded.rounds[0].text == "Hallo.\nWillkommen.\n"


def test_update_form_labels_and_option_labels(db):
    show = update_round(
        db,
        "q1",
        {
            "form_labels": {"left": "Kaffee", "right": "Tee"},
            "options": [
                {"zone": "answer_a", "label": "Kaffee"},
                {"zone": "answer_b", "label": "Tee"},
            ],
        },
    )
    q1 = show.rounds[1]
    assert q1.form_labels == {"left": "Kaffee", "right": "Tee"}
    assert [o.label for o in q1.options] == ["Kaffee", "Tee"]


def test_none_removes_field(db):
    update_round(db, "q1", {"audio": "q1.mp3"})
    show = update_round(db, "q1", {"audio": None})
    assert show.rounds[1].audio is None
    _, rows = db.load_content()
    assert rows[1].audio is None


def test_invalid_edit_leaves_rows_untouched(db):
    before = db.load_content()
    # Dropping a required scale label must fail validation, not hit the DB.
    with pytest.raises(ContentError):
        update_round(db, "q1", {"form_labels": {"left": "only one pole"}})
    assert db.load_content() == before


def test_invalid_edit_does_not_touch_other_rounds(db):
    # save_content is one wipe-and-replace transaction: a rejected edit of
    # q1 must leave every other round's row byte-identical too.
    _, before_rows = db.load_content()
    with pytest.raises(ContentError):
        update_round(db, "q1", {"options": [{"zone": "answer_a", "label": "lonely"}]})
    _, after_rows = db.load_content()
    assert after_rows == before_rows


def test_zone_validation_applies_when_zone_ids_given(db):
    # q2 is a "choice" round (no zone_layout), so its zones must exist in
    # TrackingBox's map; layout rounds like q1 use logical per-question zones.
    before = db.load_content()
    with pytest.raises(ContentError):
        update_round(
            db,
            "q2",
            {"options": [{"zone": "nope", "label": "X"}, {"zone": "answer_b", "label": "Y"}]},
            valid_zone_ids={"answer_a", "answer_b"},
        )
    assert db.load_content() == before


def test_unknown_round_and_uneditable_field_rejected(db):
    with pytest.raises(ContentError, match="unknown round"):
        update_round(db, "ghost", {"question": "?"})
    with pytest.raises(ContentError, match="not editable"):
        update_round(db, "q1", {"id": "q2"})
