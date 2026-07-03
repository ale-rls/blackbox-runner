from __future__ import annotations

import time

from server.persistence import AnswerRow, Database
from server.replay import binding_state_at, build_timeline, explain_player, scores_at


def _make_db(tmp_path):
    db = Database(str(tmp_path / "game.db"))
    session_id = db.create_session()

    db.record_binding_event(session_id, "p1", None, 1, "claim", None)
    round_id = db.create_round(session_id, 0, "r1")
    db.record_answer(
        AnswerRow(
            round_id=round_id,
            session_id=session_id,
            player_id="p1",
            zone_id="a",
            resolved="answered",
            position_x=0.1,
            position_y=0.1,
            at=time.time(),
        )
    )
    db.record_score_event(session_id, "p1", round_id, 10, "majority")
    db.record_binding_event(session_id, "p1", 1, None, "lost", None)  # p1 goes lost later
    return db, session_id, round_id


def test_build_timeline_merges_and_sorts_all_event_kinds(tmp_path):
    db, session_id, round_id = _make_db(tmp_path)
    try:
        timeline = build_timeline(db, session_id)
        assert {e.kind for e in timeline} == {"binding", "answer", "score"}
        assert [e.at for e in timeline] == sorted(e.at for e in timeline)
    finally:
        db.close()


def test_explain_player_filters_to_one_player(tmp_path):
    db, session_id, round_id = _make_db(tmp_path)
    try:
        db.record_binding_event(session_id, "p2", None, 2, "claim", None)
        history = explain_player(db, session_id, "p1")
        assert all(e.player_id == "p1" for e in history)
        # claim, answer, score, lost — every event p1 generated in _make_db.
        assert len(history) == 4
        assert [e.kind for e in history] == ["binding", "answer", "score", "binding"]
    finally:
        db.close()


def test_binding_state_at_reconstructs_a_moment_in_time(tmp_path):
    db, session_id, round_id = _make_db(tmp_path)
    try:
        events = db.load_binding_events(session_id)
        claim_at = events[0].at
        lost_at = events[1].at

        before = binding_state_at(db, session_id, claim_at - 1000)
        assert "p1" not in before

        after_claim = binding_state_at(db, session_id, claim_at)
        assert after_claim["p1"].gid == 1
        assert after_claim["p1"].state == "bound"

        after_lost = binding_state_at(db, session_id, lost_at)
        assert after_lost["p1"].gid is None
        assert after_lost["p1"].state == "lost"
    finally:
        db.close()


def test_scores_at_reconstructs_running_totals(tmp_path):
    db, session_id, round_id = _make_db(tmp_path)
    try:
        db.record_score_event(session_id, "p1", round_id, 5, "bonus")
        events = db.load_score_events(session_id)
        first_at = events[0].at
        second_at = events[1].at

        assert scores_at(db, session_id, first_at) == {"p1": 10}
        assert scores_at(db, session_id, second_at) == {"p1": 15}
    finally:
        db.close()
