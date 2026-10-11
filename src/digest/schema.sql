CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
  channel_id   TEXT NOT NULL,
  ts           TEXT NOT NULL,
  ts_us        INTEGER NOT NULL,
  event_ts     TEXT NOT NULL,
  event_ts_us  INTEGER NOT NULL,
  thread_ts    TEXT NOT NULL,
  source       TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  user_id      TEXT NOT NULL,
  text         TEXT NOT NULL,
  mentions     TEXT NOT NULL,   -- JSON array of user ids
  reactions    TEXT NOT NULL,   -- JSON array of {name, user_id, ts}
  files        TEXT NOT NULL,   -- JSON array of titles
  permalink    TEXT NOT NULL,
  edited       INTEGER NOT NULL DEFAULT 0,
  deleted      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (channel_id, ts)
);
CREATE INDEX IF NOT EXISTS events_thread ON events (channel_id, thread_ts, ts_us);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts_us);

-- External content: events keeps its rowid, and the triggers keep the index in step.
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  text, content='events', content_rowid='rowid',
  tokenize = "unicode61 tokenchars '-_'"
);
CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
  INSERT INTO events_fts (rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events BEGIN
  INSERT INTO events_fts (events_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_update AFTER UPDATE OF text ON events BEGIN
  INSERT INTO events_fts (events_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO events_fts (rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS stage_runs (
  stage          TEXT NOT NULL,
  thread_id      TEXT NOT NULL,
  last_ts        TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL,
  run_at         TEXT NOT NULL,
  status         TEXT NOT NULL,
  PRIMARY KEY (stage, thread_id, last_ts, prompt_version, model)
) WITHOUT ROWID;
