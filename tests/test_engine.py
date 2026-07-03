from __future__ import annotations

import asyncio

import pytest

from server.bindings import BindingManager
from server.content import ShowContent
from server.engine import EngineError, GameEngine, RoundState
from server.models import AudienceSummary
from server.persistence import Database
from server.tracking_client import ChangeEvent
from server.tracking_client import TrackingClient

SHOW = {
    "version": "1",
    "rounds": [
        {
            "id": "r1",
            "question": "Majority round",
            "type": "majority",
            "duration_s": 100,
            "grace_s": 100,
            "points": 10,
            "options": [{"zone": "a", "label": "A"}, {"zone": "b", "label": "B"}],
        },
        {
            "id": "r2",
            "question": "Minority round",
            "type": "minority",
            "duration_s": 100,
            "grace_s": 100,
            "points": 20,
            "options": [{"zone": "a", "label": "A"}, {"zone": "b", "label": "B"}],
        },
        {
            "id": "r3",
            "question": "Correct-zone round",
            "type": "correct_zone",
            "duration_s": 100,
            "grace_s": 100,
            "points": 15,
            "options": [{"zone": "a", "label": "A", "correct": True}, {"zone": "b", "label": "B"}],
        },
    ],
}


def _seed(tracking: TrackingClient, gid: int, zone: str | None) -> None:
    tracking._state[gid] = AudienceSummary(
        gid=gid, visible=True, floor=(0.1, 0.1), floor_valid=True, zone=zone
    )


@pytest.fixture
def db():
    database = Database(":memory:")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def tracking():
    return TrackingClient("ws://unused")


@pytest.fixture
async def bindings(db, tracking):
    session_id = db.create_session()
    return await BindingManager.load(db, session_id, tracking)


@pytest.fixture
def show():
    return ShowContent.model_validate(SHOW)


@pytest.fixture
def engine(db, bindings, tracking, show):
    return GameEngine(db, bindings.session_id, show, bindings, tracking, zone_count_interval_s=0.05)


async def _claim_bound(bindings, tracking, player_id, gid, zone):
    _seed(tracking, gid, zone)
    await bindings.claim(player_id, gid)


@pytest.mark.asyncio
async def test_start_round_emits_round_opened(engine):
    queue = engine.subscribe()
    rt = await engine.start_next_round()
    assert rt.state == RoundState.ACTIVE
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.type == "round_opened"
    assert event.payload["question"] == "Majority round"


@pytest.mark.asyncio
async def test_cannot_start_second_round_while_one_active(engine):
    await engine.start_next_round()
    with pytest.raises(EngineError):
        await engine.start_next_round()


@pytest.mark.asyncio
async def test_close_marks_zone_answers_and_absent(engine, bindings, tracking):
    await engine.start_next_round()
    await _claim_bound(bindings, tracking, "p1", 1, "a")
    await _claim_bound(bindings, tracking, "p2", 2, "b")
    await _claim_bound(bindings, tracking, "p3", 3, None)  # in an unmapped zone -> absent

    await engine.close_round()
    rt = engine.current
    assert rt.state == RoundState.CLOSING
    assert rt.answers["p1"] == ("a", "answered")
    assert rt.answers["p2"] == ("b", "answered")
    assert rt.answers["p3"] == (None, "absent")


@pytest.mark.asyncio
async def test_majority_scoring(engine, bindings, tracking):
    await engine.start_next_round()
    await _claim_bound(bindings, tracking, "p1", 1, "a")
    await _claim_bound(bindings, tracking, "p2", 2, "a")
    await _claim_bound(bindings, tracking, "p3", 3, "b")

    await engine.close_round()
    rt = await engine.reveal_round()

    assert rt.tally == {"a": 2, "b": 1}
    assert rt.winning_zones == ["a"]
    scores = await engine.scores()
    assert scores == {"p1": 10, "p2": 10}


@pytest.mark.asyncio
async def test_minority_scoring(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)
    engine = GameEngine(db, session_id, show, bindings, tracking)

    await engine.start_next_round()  # r1 majority, skip
    await engine.close_round()
    await engine.reveal_round()

    await engine.start_next_round()  # r2 minority
    await _claim_bound(bindings, tracking, "p1", 1, "a")
    await _claim_bound(bindings, tracking, "p2", 2, "b")
    await _claim_bound(bindings, tracking, "p3", 3, "b")
    await engine.close_round()
    rt = await engine.reveal_round()

    assert rt.winning_zones == ["a"]
    scores = await engine.scores()
    assert scores.get("p1") == 20
    assert "p2" not in scores or scores["p2"] == 0


@pytest.mark.asyncio
async def test_correct_zone_scoring_ignores_majority(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)
    engine = GameEngine(db, session_id, show, bindings, tracking)

    for _ in range(2):
        await engine.start_next_round()
        await engine.close_round()
        await engine.reveal_round()

    await engine.start_next_round()  # r3 correct_zone, "a" is correct
    await _claim_bound(bindings, tracking, "p1", 1, "a")  # correct, but minority
    await _claim_bound(bindings, tracking, "p2", 2, "b")
    await _claim_bound(bindings, tracking, "p3", 3, "b")
    await engine.close_round()
    rt = await engine.reveal_round()

    assert rt.winning_zones == ["a"]
    scores = await engine.scores()
    assert scores.get("p1") == 15
    assert "p2" not in scores or scores["p2"] == 0


@pytest.mark.asyncio
async def test_grace_window_upgrades_late_rebind(engine, bindings, tracking):
    await engine.start_next_round()
    await _claim_bound(bindings, tracking, "p1", 1, "a")

    # p2 hasn't claimed at all when the round closes.
    await engine.close_round()
    assert "p2" not in engine.current.answers

    # p2 claims mid-grace, standing in a valid zone -> upgraded on reveal.
    await _claim_bound(bindings, tracking, "p2", 2, "b")
    rt = await engine.reveal_round()

    assert rt.answers["p2"] == ("b", "late_grace")
    scores = await engine.scores()
    # majority round: "a" has 1 (p1), "b" has 1 (p2, via late_grace) -> tie, both win.
    assert scores.get("p1") == 10
    assert scores.get("p2") == 10


@pytest.mark.asyncio
async def test_lost_gid_at_close_is_absent_not_wrong(engine, bindings, tracking):
    await engine.start_next_round()
    await _claim_bound(bindings, tracking, "p1", 1, "a")

    await bindings.handle_tracking_event(ChangeEvent(gid=1, state=None))  # p1 goes lost
    await engine.close_round()

    assert engine.current.answers["p1"] == (None, "absent")


@pytest.mark.asyncio
async def test_auto_timer_closes_and_reveals(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    fast_show = ShowContent.model_validate(
        {
            "version": "1",
            "rounds": [
                {
                    "id": "r1",
                    "question": "Fast round",
                    "type": "majority",
                    "duration_s": 0.05,
                    "grace_s": 0.05,
                    "points": 10,
                    "options": [{"zone": "a", "label": "A"}, {"zone": "b", "label": "B"}],
                }
            ],
        }
    )
    engine = GameEngine(db, session_id, fast_show, bindings, tracking)
    await _claim_bound(bindings, tracking, "p1", 1, "a")

    rt = await engine.start_next_round()
    await asyncio.sleep(0.3)
    assert rt.state == RoundState.DONE
    scores = await engine.scores()
    assert scores.get("p1") == 10


@pytest.mark.asyncio
async def test_no_more_rounds_raises(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(
        {"version": "1", "rounds": [SHOW["rounds"][0]]}
    )
    engine = GameEngine(db, session_id, show, bindings, tracking)
    await engine.start_next_round()
    await engine.close_round()
    await engine.reveal_round()

    with pytest.raises(EngineError):
        await engine.start_next_round()


@pytest.mark.asyncio
async def test_publish_cue_reaches_subscribers(engine):
    queue = engine.subscribe()
    engine.publish_cue("ritual_prompt", {"player_id": "seat-1", "corner_zone": "a"})
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.type == "ritual_prompt"
    assert event.payload == {"player_id": "seat-1", "corner_zone": "a"}


@pytest.mark.asyncio
async def test_publish_cue_defaults_to_empty_payload(engine):
    queue = engine.subscribe()
    engine.publish_cue("blackout")
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event.type == "blackout"
    assert event.payload == {}


@pytest.mark.asyncio
async def test_current_zone_counts_empty_without_active_round(engine):
    assert engine.current_zone_counts() == {}


@pytest.mark.asyncio
async def test_zone_counts_broadcast_during_active_round(engine, bindings, tracking):
    await _claim_bound(bindings, tracking, "p1", 1, "a")
    await _claim_bound(bindings, tracking, "p2", 2, "a")
    await _claim_bound(bindings, tracking, "p3", 3, "b")

    queue = engine.subscribe()
    await engine.start_next_round()

    # Drain events until we see a zone_counts cue (round_opened comes first).
    counts = None
    for _ in range(20):
        event = await asyncio.wait_for(queue.get(), timeout=1)
        if event.type == "zone_counts":
            counts = event.payload["counts"]
            break
    assert counts == {"a": 2, "b": 1}
    assert engine.current_zone_counts() == {"a": 2, "b": 1}


@pytest.mark.asyncio
async def test_zone_counts_broadcast_stops_after_close(engine, bindings, tracking):
    await _claim_bound(bindings, tracking, "p1", 1, "a")
    await engine.start_next_round()
    await engine.close_round()

    queue = engine.subscribe()
    # No more zone_counts should arrive once the round is no longer active.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.2)


# ---------------------------------------------------------------------- #
# Phase 4: round-state crash recovery
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_with_no_rounds_starts_fresh(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)
    engine = await GameEngine.load(db, session_id, show, bindings, tracking)
    assert engine.current is None
    assert engine.has_more_rounds


@pytest.mark.asyncio
async def test_load_resumes_after_a_fully_done_round(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)

    engine = GameEngine(db, session_id, show, bindings, tracking)
    await engine.start_next_round()
    await engine.close_round()
    await engine.reveal_round()

    recovered = await GameEngine.load(db, session_id, show, bindings, tracking)
    assert recovered.current is None
    # Starting the next round should pick up r2, not repeat r1.
    rt = await recovered.start_next_round()
    assert rt.content.id == "r2"


@pytest.mark.asyncio
async def test_load_recovers_mid_round_as_closing_with_answers_preserved(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)

    engine = GameEngine(db, session_id, show, bindings, tracking)
    await _claim_bound(bindings, tracking, "p1", 1, "a")
    await _claim_bound(bindings, tracking, "p2", 2, "a")
    await _claim_bound(bindings, tracking, "p3", 3, "b")
    await engine.start_next_round()
    await engine.close_round()  # crash happens here, before reveal

    recovered = await GameEngine.load(db, session_id, show, bindings, tracking)
    assert recovered.current is not None
    assert recovered.current.state == RoundState.CLOSING
    assert recovered.current.content.id == "r1"
    assert recovered.current.answers["p1"] == ("a", "answered")
    assert recovered.current.answers["p2"] == ("a", "answered")
    assert recovered.current.answers["p3"] == ("b", "answered")

    # No answers were lost, and an operator can still finish the round.
    rt = await recovered.reveal_round()
    assert rt.state == RoundState.DONE
    scores = await recovered.scores()
    assert scores.get("p1") == 10
    assert scores.get("p2") == 10
    assert "p3" not in scores or scores["p3"] == 0


@pytest.mark.asyncio
async def test_load_does_not_repeat_a_recovered_round(db, tracking):
    session_id = db.create_session()
    bindings = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)

    engine = GameEngine(db, session_id, show, bindings, tracking)
    await engine.start_next_round()  # r1
    await engine.close_round()

    recovered = await GameEngine.load(db, session_id, show, bindings, tracking)
    await recovered.reveal_round()  # finish r1
    rt = await recovered.start_next_round()
    assert rt.content.id == "r2"  # not r1 again


# ---------------------------------------------------------------------- #
# Phase 5: content hot-reload
# ---------------------------------------------------------------------- #

NEW_SHOW = {
    "version": "2",
    "rounds": [
        {
            "id": "r1-updated",
            "question": "Updated question",
            "type": "majority",
            "duration_s": 30,
            "grace_s": 5,
            "points": 5,
            "options": [{"zone": "a", "label": "A"}, {"zone": "b", "label": "B"}],
        }
    ],
}


@pytest.mark.asyncio
async def test_reload_show_between_rounds(engine):
    assert engine.show.rounds[0].id == "r1"
    engine.reload_show(ShowContent.model_validate(NEW_SHOW))
    assert engine.show.rounds[0].id == "r1-updated"
    assert len(engine.show.rounds) == 1


@pytest.mark.asyncio
async def test_reload_show_refused_mid_round(engine):
    await engine.start_next_round()
    with pytest.raises(EngineError):
        engine.reload_show(ShowContent.model_validate(NEW_SHOW))
    assert engine.show.rounds[0].id == "r1"  # unchanged


@pytest.mark.asyncio
async def test_reload_show_allowed_after_round_done(engine):
    await engine.start_next_round()
    await engine.close_round()
    await engine.reveal_round()
    engine.reload_show(ShowContent.model_validate(NEW_SHOW))
    assert engine.show.rounds[0].id == "r1-updated"
