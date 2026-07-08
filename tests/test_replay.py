from __future__ import annotations

import time

import pytest

from server.pocketbase_client import AnswerRow
from server.replay import binding_state_at, build_timeline, explain_player, scores_at


async def _seed_db(pb):
    session_id = await pb.create_session()

    await pb.record_binding_event(session_id, "p1", None, 1, "claim", None)
    round_id = await pb.create_round(session_id, 0, "r1")
    await pb.record_answer(
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
    await pb.record_score_event(session_id, "p1", round_id, 10, "majority")
    await pb.record_binding_event(session_id, "p1", 1, None, "lost", None)  # p1 goes lost later
    return session_id, round_id


@pytest.mark.asyncio
async def test_build_timeline_merges_and_sorts_all_event_kinds(pb):
    session_id, round_id = await _seed_db(pb)
    timeline = await build_timeline(pb, session_id)
    assert {e.kind for e in timeline} == {"binding", "answer", "score"}
    assert [e.at for e in timeline] == sorted(e.at for e in timeline)


@pytest.mark.asyncio
async def test_explain_player_filters_to_one_player(pb):
    session_id, round_id = await _seed_db(pb)
    await pb.record_binding_event(session_id, "p2", None, 2, "claim", None)
    history = await explain_player(pb, session_id, "p1")
    assert all(e.player_id == "p1" for e in history)
    # claim, answer, score, lost — every event p1 generated in _seed_db.
    assert len(history) == 4
    assert [e.kind for e in history] == ["binding", "answer", "score", "binding"]


@pytest.mark.asyncio
async def test_binding_state_at_reconstructs_a_moment_in_time(pb):
    session_id, round_id = await _seed_db(pb)
    events = await pb.load_binding_events(session_id)
    claim_at = events[0].at
    lost_at = events[1].at

    before = await binding_state_at(pb, session_id, claim_at - 1000)
    assert "p1" not in before

    after_claim = await binding_state_at(pb, session_id, claim_at)
    assert after_claim["p1"].gid == 1
    assert after_claim["p1"].state == "bound"

    after_lost = await binding_state_at(pb, session_id, lost_at)
    assert after_lost["p1"].gid is None
    assert after_lost["p1"].state == "lost"


@pytest.mark.asyncio
async def test_scores_at_reconstructs_running_totals(pb):
    session_id, round_id = await _seed_db(pb)
    await pb.record_score_event(session_id, "p1", round_id, 5, "bonus")
    events = await pb.load_score_events(session_id)
    first_at = events[0].at
    second_at = events[1].at

    assert await scores_at(pb, session_id, first_at) == {"p1": 10}
    assert await scores_at(pb, session_id, second_at) == {"p1": 15}
