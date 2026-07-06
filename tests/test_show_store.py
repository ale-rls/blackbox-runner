"""show_store: surgical YAML edits that keep operator comments intact and
never write an invalid show to disk."""

from __future__ import annotations

import pytest

from server.content import ContentError
from server.show_store import update_round

SHOW = """\
version: "2"
rounds:
  # ---------------------------------------------------------------- #
  # K2 — Intro
  # ---------------------------------------------------------------- #
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
"""


@pytest.fixture
def show_path(tmp_path):
    path = tmp_path / "show.yaml"
    path.write_text(SHOW)
    return path


def test_update_question_preserves_comments_and_other_rounds(show_path):
    show = update_round(show_path, "q1", {"question": "Beer or wine?"})

    assert [r.id for r in show.rounds] == ["intro", "q1"]
    assert show.rounds[1].question == "Beer or wine?"
    text = show_path.read_text()
    assert "# K2 — Intro" in text  # comments survive the round-trip
    assert "Beer or wine?" in text
    assert show.rounds[0].text == "Hallo.\nWillkommen.\n"


def test_update_multiline_text_stays_literal_block(show_path):
    update_round(show_path, "intro", {"text": "Neue Zeile eins.\nNeue Zeile zwei."})
    text = show_path.read_text()
    assert "text: |" in text
    assert "Neue Zeile eins." in text


def test_update_form_labels_and_option_labels(show_path):
    show = update_round(
        show_path,
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


def test_none_removes_field(show_path):
    update_round(show_path, "q1", {"audio": "q1.mp3"})
    show = update_round(show_path, "q1", {"audio": None})
    assert show.rounds[1].audio is None
    assert "q1.mp3" not in show_path.read_text()


def test_invalid_edit_leaves_file_untouched(show_path):
    before = show_path.read_text()
    # Dropping a required scale label must fail validation, not hit disk.
    with pytest.raises(ContentError):
        update_round(show_path, "q1", {"form_labels": {"left": "only one pole"}})
    assert show_path.read_text() == before


def test_zone_validation_applies_when_zone_ids_given(show_path):
    before = show_path.read_text()
    with pytest.raises(ContentError):
        update_round(
            show_path,
            "q1",
            {"options": [{"zone": "nope", "label": "X"}, {"zone": "answer_b", "label": "Y"}]},
            valid_zone_ids={"answer_a", "answer_b"},
        )
    assert show_path.read_text() == before


def test_unknown_round_and_uneditable_field_rejected(show_path):
    with pytest.raises(ContentError, match="unknown round"):
        update_round(show_path, "ghost", {"question": "?"})
    with pytest.raises(ContentError, match="not editable"):
        update_round(show_path, "q1", {"id": "q2"})
