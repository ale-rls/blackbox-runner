# TouchDesigner — WebSocket DAT callbacks for the game server's /ws/td cue stream.
#
# Attach this as the *Callbacks DAT* of a WebSocket DAT pointed at
#   ws://localhost:8100/ws/td
# See docs/touchdesigner.md for the full cue vocabulary.
#
# The server sends:
#   * once on connect: {"type":"hello","round":{...}|null,"zone_counts":{...},"zones":{...}}
#   * round_opened / round_closing / answers_locked: {"type":..., "round_id", "index",
#       "state", "question", "options":[{"zone","label"}], "duration_s", "grace_s",
#       "opened_at", "closed_at"}
#   * zone_counts (every ~1s while a round is active): {"type":"zone_counts",
#       "round_id", "counts": {zone_id: count}}
#   * reveal: round fields + "tally": {zone_id: count}, "winning_zones": [zone_id, ...]
#   * scores_updated: {"type":"scores_updated", "scores": {player_id: points}}
#   * any other named cue an operator fires by hand (e.g. "ritual_prompt")
#
# This file runs inside TouchDesigner's Python (it references op()); it is
# not part of the Python package and is not unit-tested here.

import json

ROUND_TABLE = 'round'
ZONE_COUNTS_TABLE = 'zone_counts'
SCORES_TABLE = 'scores'

ROUND_FIELDS = [
    'round_id', 'index', 'state', 'question', 'duration_s', 'grace_s',
    'opened_at', 'closed_at',
]


def _table(name):
    return op(name)


def _set_round(round_data):
    t = _table(ROUND_TABLE)
    if t is None:
        return
    t.clear()
    t.appendRow(['field', 'value'])
    if not round_data:
        return
    for field in ROUND_FIELDS:
        t.appendRow([field, round_data.get(field, '')])
    # Options and any reveal-only fields (tally, winning_zones) go in as JSON
    # blobs — TD-side scripting can json.loads() a specific cell as needed.
    if 'options' in round_data:
        t.appendRow(['options', json.dumps(round_data['options'])])
    if 'tally' in round_data:
        t.appendRow(['tally', json.dumps(round_data['tally'])])
    if 'winning_zones' in round_data:
        t.appendRow(['winning_zones', json.dumps(round_data['winning_zones'])])


def _set_zone_counts(counts):
    t = _table(ZONE_COUNTS_TABLE)
    if t is None:
        return
    t.clear()
    t.appendRow(['zone', 'count'])
    for zone, count in (counts or {}).items():
        t.appendRow([zone, count])


def _set_scores(scores):
    t = _table(SCORES_TABLE)
    if t is None:
        return
    t.clear()
    t.appendRow(['player_id', 'points'])
    for player_id, points in sorted((scores or {}).items(), key=lambda kv: -kv[1]):
        t.appendRow([player_id, points])


def onReceiveText(dat, rowIndex, message):
    try:
        msg = json.loads(message)
    except Exception:
        return
    if not isinstance(msg, dict):
        return

    cue = msg.get('type')
    if cue == 'hello':
        _set_round(msg.get('round'))
        _set_zone_counts(msg.get('zone_counts'))
    elif cue in ('round_opened', 'round_closing', 'answers_locked', 'reveal'):
        _set_round(msg)
    elif cue == 'zone_counts':
        _set_zone_counts(msg.get('counts'))
    elif cue == 'scores_updated':
        _set_scores(msg.get('scores'))
    # Other cues (e.g. 'ritual_prompt') are free-form — wire additional
    # elif branches here as the show's cue vocabulary grows.
    return


# The server resends a full 'hello' on every reconnect, so stale tables
# are corrected automatically once the WebSocket DAT reconnects.
def onDisconnect(dat):
    return
