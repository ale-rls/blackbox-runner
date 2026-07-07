#!/usr/bin/env python3
"""Import the authoring copy content/show.yaml into the game database.

The DB (content_rounds/content_meta) is what the server actually loads;
show.yaml stays the git-tracked authoring source. Run this after every edit
that must go live — it validates the whole show first and writes nothing on
failure. Structural changes (add/remove/reorder rounds, option zone
mappings) can ONLY be applied this way; the admin console edits existing
rounds' text/timing/labels in place.

Usage:
    python scripts/import_content.py [--content content/show.yaml] \
        [--db data/blackbox-runner.db] [--tracking-http http://localhost:8000] [--dry-run]

--tracking-http additionally validates option zones against a running
TrackingBox's real zone map (same check as scripts/validate_content.py);
without it, zone ids are not cross-checked.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import content_db  # noqa: E402
from server.content import ContentError, validate_show  # noqa: E402
from server.models import ZoneMap  # noqa: E402
from server.persistence import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", default="content/show.yaml")
    parser.add_argument("--db", default="data/blackbox-runner.db")
    parser.add_argument(
        "--tracking-http",
        default=None,
        help="validate option zones against this TrackingBox's /api/zones",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate only, write nothing")
    args = parser.parse_args()

    valid_zone_ids = None
    if args.tracking_http:
        zones_url = f"{args.tracking_http.rstrip('/')}/api/zones"
        try:
            with urllib.request.urlopen(zones_url, timeout=5) as resp:
                valid_zone_ids = ZoneMap.model_validate_json(resp.read()).zone_ids()
        except Exception as exc:
            print(f"Could not reach TrackingBox at {zones_url}: {exc}", file=sys.stderr)
            return 2

    try:
        raw = yaml.safe_load(Path(args.content).read_text())
        show = validate_show(
            raw, valid_zone_ids=valid_zone_ids, source=f"show content at {args.content}"
        )
    except FileNotFoundError as exc:
        print(f"Cannot read {args.content}: {exc}", file=sys.stderr)
        return 2
    except ContentError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    # Rows are built from the raw YAML (not the validated model dump) so
    # zone_layout stays "as authored" — see content_db.raw_to_row.
    rows = content_db.rows_from_raw(raw.get("rounds") or [])
    version = show.version

    if args.dry_run:
        print(f"OK (dry run): {args.content} — version {version!r}, {len(rows)} round(s); not written")
        return 0

    db = Database(args.db)
    try:
        db.save_content(version, rows)
    finally:
        db.close()
    print(f"Imported {len(rows)} round(s) (version {version!r}) from {args.content} into {args.db}")
    print("If the server is running, apply with: POST /api/admin/content/reload")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
