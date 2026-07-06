"""Structured edits to content/show.yaml from the admin console.

The YAML file stays the single source of truth for show prep (it's what the
content freeze in docs/runbook.md versions), so browser edits are applied as
surgical field updates to the file itself — via ruamel.yaml round-tripping,
which preserves the operator's comments and section headers — never by
re-dumping the pydantic model.

Every edit is validated as a complete show (same rules as startup loading)
before a byte hits disk; an invalid edit leaves the file untouched.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from .content import ContentError, ShowContent, validate_show

# Fields the admin editor may change. id is the round's identity (audio
# filenames and persisted answer rows key on it) and stays hand-edited.
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


def _ruamel() -> YAML:
    y = YAML()  # round-trip mode: keeps comments, ordering, block styles
    y.preserve_quotes = True
    y.width = 100_000  # never re-wrap the operator's long lines
    # Match the file's hand-written style (dash indented under "rounds:"),
    # so an edit diffs as one line, not a whole-file re-indent.
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _styled(value: object) -> object:
    # Multiline strings (narration text) stay readable literal blocks.
    if isinstance(value, str) and "\n" in value:
        return LiteralScalarString(value)
    return value


def update_round(
    path: str | Path,
    round_id: str,
    fields: dict,
    *,
    valid_zone_ids: Optional[set[str]] = None,
) -> ShowContent:
    """Merge ``fields`` into one round of the YAML file and write it back.

    Returns the validated post-edit show. Raises ContentError for unknown
    rounds/fields or if the merged show fails validation (file unchanged).
    """
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ContentError(f"field(s) not editable: {sorted(unknown)}")

    path = Path(path)
    y = _ruamel()
    doc = y.load(path.read_text())

    rounds = (doc or {}).get("rounds") or []
    target = next((r for r in rounds if r.get("id") == round_id), None)
    if target is None:
        raise ContentError(f"unknown round {round_id!r}")

    for key, value in fields.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = _styled(value)

    buf = io.StringIO()
    y.dump(doc, buf)
    text = buf.getvalue()

    show = validate_show(
        pyyaml.safe_load(text), valid_zone_ids=valid_zone_ids, source=f"edited show at {path}"
    )
    path.write_text(text)
    return show
