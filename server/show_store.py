"""Structured edits to the DB-stored show from the admin console.

The content_rounds collection is the runtime source of truth (seeded from
the authoring copy content/show.yaml via scripts/import_content.py);
browser edits merge fields into one round and rewrite the whole show.

Every edit is validated as a complete show (same rules as startup loading)
before anything is written; an invalid edit leaves the stored rows
untouched. Unlike the old SQLite layer, PocketBase has no cross-record
transaction, so the rewrite itself has a small non-atomic window — the
client's save_content re-checks the stored count immediately after so a
partial replace fails loudly instead of silently (documented MVP gap).
"""

from __future__ import annotations

from typing import Optional

from . import content_db
from .content import ContentError, ShowContent, validate_show
from .pocketbase_client import PocketBaseClient

# Fields the admin editor may change. id is the round's identity (audio
# filenames and persisted answer rows key on it) and stays authoring-only,
# as do structural changes (add/remove/reorder rounds, option zones) — those
# go through show.yaml + scripts/import_content.py.
EDITABLE_FIELDS = frozenset(
    {
        "question",
        "text",
        "audio",
        "type",
        "form",
        "form_labels",
        "options",
        "duration_s",
        "grace_s",
        "points",
    }
)


async def update_round(
    db: PocketBaseClient,
    round_id: str,
    fields: dict,
    *,
    valid_zone_ids: Optional[set[str]] = None,
    audio_dir: Optional[str] = None,
) -> ShowContent:
    """Merge ``fields`` into one round of the stored show and write it back.

    Returns the validated post-edit show. Raises ContentError for unknown
    rounds/fields or if the merged show fails validation (rows unchanged).
    A field set to None is removed, i.e. reset to its content.py default.
    """
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ContentError(f"field(s) not editable: {sorted(unknown)}")

    version, rows = await db.load_content()
    rounds = [content_db.row_to_raw(r) for r in rows]
    target = next((r for r in rounds if r["id"] == round_id), None)
    if target is None:
        raise ContentError(f"unknown round {round_id!r}")

    for key, value in fields.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value

    raw: dict = {"rounds": rounds}
    if version:
        raw["version"] = version
    show = validate_show(
        raw, valid_zone_ids=valid_zone_ids, source="edited show content (database)"
    )
    await db.save_content(version, content_db.rows_from_raw(rounds), audio_dir=audio_dir)
    return show


async def create_round(
    db: PocketBaseClient,
    new_round: dict,
    *,
    after_id: Optional[str] = None,
    valid_zone_ids: Optional[set[str]] = None,
    audio_dir: Optional[str] = None,
) -> ShowContent:
    """Insert a new round (a raw dict, same shape as one show.yaml entry)
    into the stored show and write it back.

    Placed at the end unless ``after_id`` names an existing round to insert
    after. Raises ContentError for a missing/duplicate id, an unknown
    ``after_id``, or if the resulting show fails validation (rows
    unchanged).
    """
    round_id = new_round.get("id")
    if not round_id:
        raise ContentError("new round needs an id")

    version, rows = await db.load_content()
    rounds = [content_db.row_to_raw(r) for r in rows]
    if any(r["id"] == round_id for r in rounds):
        raise ContentError(f"round id {round_id!r} already exists")

    if after_id is None:
        rounds.append(new_round)
    else:
        idx = next((i for i, r in enumerate(rounds) if r["id"] == after_id), None)
        if idx is None:
            raise ContentError(f"unknown round {after_id!r}")
        rounds.insert(idx + 1, new_round)

    raw: dict = {"rounds": rounds}
    if version:
        raw["version"] = version
    show = validate_show(
        raw, valid_zone_ids=valid_zone_ids, source="edited show content (database)"
    )
    await db.save_content(version, content_db.rows_from_raw(rounds), audio_dir=audio_dir)
    return show


async def delete_round(
    db: PocketBaseClient,
    round_id: str,
    *,
    valid_zone_ids: Optional[set[str]] = None,
    audio_dir: Optional[str] = None,
) -> ShowContent:
    """Remove one round from the stored show and write it back.

    Raises ContentError for an unknown round id or if the resulting show
    fails validation (rows unchanged) — e.g. this was the last remaining
    reference some other check depended on.
    """
    version, rows = await db.load_content()
    rounds = [content_db.row_to_raw(r) for r in rows]
    remaining = [r for r in rounds if r["id"] != round_id]
    if len(remaining) == len(rounds):
        raise ContentError(f"unknown round {round_id!r}")

    raw: dict = {"rounds": remaining}
    if version:
        raw["version"] = version
    show = validate_show(
        raw, valid_zone_ids=valid_zone_ids, source="edited show content (database)"
    )
    await db.save_content(version, content_db.rows_from_raw(remaining), audio_dir=audio_dir)
    return show
