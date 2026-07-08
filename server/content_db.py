"""DB-backed show content: content_rounds/content_meta are the runtime
source of truth for rounds, questions, and the zone->answer mapping.

content/show.yaml remains the git-tracked authoring copy — content owners
edit it in a text editor and push it into the DB with
scripts/import_content.py. content.py's pydantic models and validate_show()
stay the single validation path; this module only converts between DB rows
and the raw dicts those models validate.
"""

from __future__ import annotations

from typing import Optional

from .content import ShowContent, validate_show
from .pocketbase_client import ContentRoundRow, PocketBaseClient


def row_to_raw(row: ContentRoundRow) -> dict:
    """A stored round as the raw dict validate_show() consumes. Optional
    fields (text/audio/zone_layout) are omitted when unset so pydantic
    defaults and layout derivation behave exactly as with authored YAML."""
    raw = {
        "id": row.round_id,
        "question": row.question,
        "type": row.type,
        "duration_s": row.duration_s,
        "grace_s": row.grace_s,
        "points": row.points,
        "form": row.form,
        "form_labels": row.form_labels,
        "options": row.options,
    }
    if row.text is not None:
        raw["text"] = row.text
    if row.audio is not None:
        raw["audio"] = row.audio
    if row.zone_layout is not None:
        raw["zone_layout"] = row.zone_layout
    return raw


def raw_to_row(ord_: int, raw: dict) -> ContentRoundRow:
    """One authored round dict -> a DB row. Defaults mirror RoundContent's,
    so an author omitting a field and an author writing the default store
    identically. zone_layout is stored only as authored — never the value
    derived from form — so editing the form later re-derives the layout."""
    return ContentRoundRow(
        round_id=raw["id"],
        ord=ord_,
        question=raw.get("question", ""),
        type=raw.get("type", "majority"),
        duration_s=float(raw.get("duration_s", 20.0)),
        grace_s=float(raw.get("grace_s", 5.0)),
        points=int(raw.get("points", 10)),
        text=raw.get("text"),
        audio=raw.get("audio"),
        form=raw.get("form", "choice"),
        zone_layout=raw.get("zone_layout"),
        form_labels=dict(raw.get("form_labels") or {}),
        options=[dict(o) for o in (raw.get("options") or [])],
    )


def rows_from_raw(rounds: list[dict]) -> list[ContentRoundRow]:
    return [raw_to_row(i, r) for i, r in enumerate(rounds)]


async def load_show_db(
    db: PocketBaseClient, *, valid_zone_ids: Optional[set[str]] = None
) -> ShowContent:
    """Load and validate the show from the database. An empty content_rounds
    collection is a valid empty show (no rounds imported yet), not an error."""
    version, rows = await db.load_content()
    raw: dict = {"rounds": [row_to_raw(r) for r in rows]}
    if version:
        raw["version"] = version
    return validate_show(raw, valid_zone_ids=valid_zone_ids, source="show content (database)")
