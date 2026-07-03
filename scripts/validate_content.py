#!/usr/bin/env python3
"""Validate content/show.yaml against a running TrackingBox's real zone map.

Run this before freezing content for a show — it's the same validation the
game server does at startup, but standalone, so content can be checked
without restarting anything.

Usage:
    python scripts/validate_content.py [--content content/show.yaml] [--tracking-http http://localhost:8000]
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.content import ContentError, load_show  # noqa: E402
from server.models import ZoneMap  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", default="content/show.yaml")
    parser.add_argument("--tracking-http", default="http://localhost:8000")
    args = parser.parse_args()

    zones_url = f"{args.tracking_http.rstrip('/')}/api/zones"
    try:
        with urllib.request.urlopen(zones_url, timeout=5) as resp:
            zones = ZoneMap.model_validate_json(resp.read())
    except Exception as exc:
        print(f"Could not reach TrackingBox at {zones_url}: {exc}", file=sys.stderr)
        print("Is it running? (audience-tracker serve --config config.json)", file=sys.stderr)
        return 2

    try:
        show = load_show(args.content, valid_zone_ids=zones.zone_ids())
    except ContentError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {args.content} — {len(show.rounds)} round(s) validated against {len(zones.zone_ids())} live zone(s)")
    for r in show.rounds:
        options = ", ".join(f"{o.zone} ({o.label})" for o in r.options)
        print(f"  [{r.type:12s}] {r.id}: {r.question!r} -> {options}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
