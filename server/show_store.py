"""Structured edits to the DB-stored show from the admin console.

The content_rounds table is the runtime source of truth (seeded from the
authoring copy content/show.yaml via scripts/import_content.py); browser
edits merge fields into one round and rewrite the whole show in a single
transaction.

Every edit is validated as a complete show (same rules as startup loading)
before anything is written; an invalid edit leaves the stored rows untouched.
"""

from __future__ import annotations

from typing import Optional

from . import content_db
from .content import ContentError, ShowContent, validate_show
from .persistence import Database

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


def update_round(
    db: Database,
    round_id: str,
    fields: dict,
    *,
    valid_zone_ids: Optional[set[str]] = None,
) -> ShowContent:
    """Merge ``fields`` into one round of the stored show and write it back.

    Returns the validated post-edit show. Raises ContentError for unknown
    rounds/fields or if the merged show fails validation (rows unchanged).
    A field set to None is removed, i.e. reset to its content.py default.
    """
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ContentError(f"field(s) not editable: {sorted(unknown)}")

    version, rows = db.load_content()
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
    db.save_content(version, content_db.rows_from_raw(rounds))
    return show
