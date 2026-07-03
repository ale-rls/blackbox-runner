from __future__ import annotations

from server.persistence import Database, PlayerRow


def test_session_lifecycle(tmp_path):
    db = Database(str(tmp_path / "game.db"))
    try:
        assert db.get_active_session_id() is None
        session_id = db.create_session(content_version="v1")
        assert session_id == db.get_active_session_id()
    finally:
        db.close()


def test_player_upsert_and_load_roundtrip(tmp_path):
    db = Database(str(tmp_path / "game.db"))
    try:
        session_id = db.create_session()
        row = PlayerRow(
            id="p1",
            session_id=session_id,
            gid=42,
            display_name="Alex",
            state="bound",
            last_seen_x=0.5,
            last_seen_y=0.25,
            last_seen_at=123.0,
        )
        db.upsert_player(row)
        loaded = db.load_players(session_id)
        assert len(loaded) == 1
        assert loaded[0] == row

        # Upsert again with a new gid: same primary key, row updates in place.
        row.gid = 99
        row.state = "lost"
        db.upsert_player(row)
        loaded = db.load_players(session_id)
        assert len(loaded) == 1
        assert loaded[0].gid == 99
        assert loaded[0].state == "lost"
    finally:
        db.close()


def test_binding_events_audit_trail(tmp_path):
    db = Database(str(tmp_path / "game.db"))
    try:
        session_id = db.create_session()
        db.record_binding_event(session_id, "p1", None, 42, "claim", actor=None)
        db.record_binding_event(session_id, "p1", 42, None, "lost", actor=None)
        db.record_binding_event(session_id, "p1", None, 43, "operator", actor="usher-1")

        events = db.load_binding_events(session_id)
        assert [e.reason for e in events] == ["claim", "lost", "operator"]
        assert events[-1].actor == "usher-1"
        assert events[-1].new_gid == 43
    finally:
        db.close()


def test_reopening_database_preserves_wal_data(tmp_path):
    path = str(tmp_path / "game.db")
    db = Database(path)
    session_id = db.create_session()
    db.upsert_player(
        PlayerRow(
            id="p1",
            session_id=session_id,
            gid=1,
            display_name=None,
            state="bound",
            last_seen_x=None,
            last_seen_y=None,
            last_seen_at=None,
        )
    )
    db.close()

    reopened = Database(path)
    try:
        assert reopened.get_active_session_id() == session_id
        rows = reopened.load_players(session_id)
        assert len(rows) == 1
        assert rows[0].gid == 1
    finally:
        reopened.close()
