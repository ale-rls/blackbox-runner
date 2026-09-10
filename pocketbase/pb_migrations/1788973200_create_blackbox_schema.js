/// <reference path="../pb_data/types.d.ts" />

// Baseline migration for the schema previously created by
// scripts/pocketbase_bootstrap.py. `ensure` makes this safe to deploy over an
// existing manually bootstrapped database: existing collections and records
// are adopted, while a fresh database gets the complete schema.
//
// The down migration is intentionally a no-op. Rolling this baseline back by
// deleting collections would destroy show and audience data.
migrate((app) => {
  function existing(name) {
    try {
      return app.findCollectionByNameOrId(name);
    } catch (_) {
      return null;
    }
  }

  function ensure(definition) {
    const found = existing(definition.name);
    if (found) return found;

    const collection = new Collection(definition);
    app.save(collection);
    return collection;
  }

  function relation(name, collectionId) {
    return {
      type: "relation",
      name,
      collectionId,
      maxSelect: 1,
      cascadeDelete: false,
    };
  }

  function field(type, name) {
    return { type, name };
  }

  const sessions = ensure({
    type: "base",
    name: "sessions",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      field("number", "started_at"),
      field("text", "content_version"),
      field("text", "status"),
    ],
  });

  const rounds = ensure({
    type: "base",
    name: "rounds",
    listRule: "",
    viewRule: "",
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      relation("session", sessions.id),
      field("number", "idx"),
      field("text", "question_id"),
      field("text", "state"),
      field("json", "opened_at"),
      field("json", "closed_at"),
      field("json", "payload"),
    ],
  });

  ensure({
    type: "base",
    name: "players",
    listRule: "",
    viewRule: "",
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      relation("session", sessions.id),
      field("text", "player_key"),
      field("json", "gid"),
      field("text", "display_name"),
      field("text", "state"),
      field("json", "last_seen_x"),
      field("json", "last_seen_y"),
      field("json", "last_seen_at"),
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_players_session_key ON players (session, player_key)",
    ],
  });

  ensure({
    type: "base",
    name: "binding_events",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      relation("session", sessions.id),
      field("text", "player_key"),
      field("json", "old_gid"),
      field("json", "new_gid"),
      field("text", "reason"),
      field("text", "actor"),
      field("number", "at"),
    ],
  });

  ensure({
    type: "base",
    name: "answers",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      relation("round", rounds.id),
      relation("session", sessions.id),
      field("text", "player_key"),
      field("text", "zone_id"),
      field("text", "resolved"),
      field("json", "position_x"),
      field("json", "position_y"),
      field("number", "at"),
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_answers_round_player ON answers (round, player_key)",
    ],
  });

  ensure({
    type: "base",
    name: "score_events",
    listRule: "",
    viewRule: "",
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      relation("session", sessions.id),
      field("text", "player_key"),
      relation("round", rounds.id),
      field("number", "points"),
      field("text", "reason"),
      field("number", "at"),
    ],
  });

  ensure({
    type: "base",
    name: "content_meta",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [field("text", "version")],
  });

  ensure({
    type: "base",
    name: "content_rounds",
    listRule: null,
    viewRule: null,
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      field("text", "round_id"),
      field("number", "ord"),
      field("text", "question"),
      field("text", "type"),
      field("number", "duration_s"),
      field("number", "grace_s"),
      field("number", "points"),
      field("text", "text"),
      field("text", "audio"),
      field("text", "form"),
      field("text", "zone_layout"),
      field("json", "form_labels"),
      field("json", "options"),
      {
        type: "file",
        name: "audio_file",
        maxSelect: 1,
        maxSize: 52428800,
      },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_content_rounds_rid ON content_rounds (round_id)",
    ],
  });

  ensure({
    type: "base",
    name: "game_state",
    listRule: "",
    viewRule: "",
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      field("text", "session_id"),
      field("json", "available_gids"),
      field("number", "updated_at"),
    ],
  });

  ensure({
    type: "base",
    name: "claim_requests",
    listRule: "",
    viewRule: "",
    createRule: "",
    updateRule: null,
    deleteRule: null,
    fields: [
      field("text", "player_key"),
      field("json", "gid"),
      field("text", "display_name"),
      field("text", "status"),
      field("text", "detail"),
      field("number", "at"),
    ],
  });

  ensure({
    type: "base",
    name: "player_reveals",
    listRule: "",
    viewRule: "",
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      relation("session", sessions.id),
      relation("round", rounds.id),
      field("text", "player_key"),
      field("text", "zone"),
      field("text", "resolved"),
      field("number", "at"),
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_player_reveals_round_player ON player_reveals (round, player_key)",
    ],
  });

  ensure({
    type: "base",
    name: "live_stats",
    listRule: "",
    viewRule: "",
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      field("text", "session_id"),
      field("text", "round_id"),
      field("json", "zone_counts"),
      field("number", "updated_at"),
    ],
  });
}, (_app) => {
  // Deliberately irreversible: never delete production collections/data.
});
