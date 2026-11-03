"""SQLite v1, explicit independent of Python dataclass layouts. No migrations yet."""

SCHEMA_VERSION = 1
APPLICATION_ID = 0x47454E31  # GEN1

# v1 DDL is intentionally unchanged: API boundaries enforce NUL-free UTF-8
# nonblank text before these SQL minimum-content checks. With NUL excluded,
# length(trim(TEXT)) does not truncate otherwise valid text. No implicit schema
# migration or rewrite of existing datasets is performed by this review fix.

DDL = (
    """CREATE TABLE companion (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        companion_id TEXT NOT NULL UNIQUE,
        created_utc TEXT NOT NULL,
        day0_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id)
            DEFERRABLE INITIALLY DEFERRED,
        day0_sha256 TEXT NOT NULL CHECK(length(day0_sha256) = 64)
    ) STRICT""",
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        companion_slot INTEGER NOT NULL DEFAULT 1 CHECK(companion_slot = 1)
            REFERENCES companion(singleton),
        created_utc TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0)
    ) STRICT""",
    """CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        companion_slot INTEGER NOT NULL DEFAULT 1 CHECK(companion_slot = 1)
            REFERENCES companion(singleton) DEFERRABLE INITIALLY DEFERRED,
        session_id TEXT REFERENCES sessions(session_id),
        kind TEXT NOT NULL CHECK(kind IN ('day0','user_input','model_reply','tool_echo','summary','inference')),
        origin TEXT NOT NULL CHECK(origin IN ('host_initialization','user_input','model_output','tool_result')),
        root_event_id TEXT NOT NULL REFERENCES events(event_id) DEFERRABLE INITIALLY DEFERRED,
        parent_event_id TEXT REFERENCES events(event_id),
        content TEXT NOT NULL CHECK(length(trim(content)) > 0),
        recorded_utc TEXT NOT NULL,
        CHECK((kind = 'day0' AND session_id IS NULL AND origin = 'host_initialization'
               AND root_event_id = event_id AND parent_event_id IS NULL)
           OR (session_id IS NOT NULL AND (
               (kind = 'user_input' AND origin = 'user_input' AND root_event_id = event_id AND parent_event_id IS NULL)
            OR (kind = 'model_reply' AND origin = 'model_output' AND root_event_id = event_id AND parent_event_id IS NULL)
            OR (kind = 'tool_echo' AND origin = 'tool_result' AND parent_event_id IS NOT NULL)
            OR (kind IN ('summary','inference') AND origin = 'model_output' AND parent_event_id IS NOT NULL))))
    ) STRICT""",
    "CREATE UNIQUE INDEX one_day0 ON events(kind) WHERE kind = 'day0'",
    """CREATE TRIGGER derived_provenance BEFORE INSERT ON events
        WHEN NEW.parent_event_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM events p JOIN events r ON r.event_id = p.root_event_id
                WHERE p.event_id = NEW.parent_event_id AND p.root_event_id = NEW.root_event_id
                  AND p.session_id = NEW.session_id AND r.session_id = NEW.session_id
                  AND r.event_id = r.root_event_id
            ) THEN RAISE(ABORT, 'invalid derived provenance') END;
        END""",
    """CREATE TABLE requests (
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        input_id TEXT NOT NULL,
        user_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
        PRIMARY KEY(session_id, input_id)
    ) STRICT""",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        input_id TEXT NOT NULL,
        base_revision INTEGER NOT NULL CHECK(base_revision >= 0),
        state TEXT NOT NULL CHECK(state IN ('running','failed','interrupted','unknown','stale','committed')),
        error_code TEXT CHECK(error_code IN ('model_error','unexpected_model_error','generation_interrupted',
                                          'retry_superseded','stale_context')),
        started_utc TEXT NOT NULL,
        finished_utc TEXT,
        UNIQUE(attempt_id, session_id, input_id),
        FOREIGN KEY(session_id, input_id) REFERENCES requests(session_id, input_id),
        CHECK((state = 'running' AND finished_utc IS NULL AND error_code IS NULL)
           OR (state = 'committed' AND finished_utc IS NOT NULL AND error_code IS NULL)
           OR (state IN ('failed','interrupted','unknown','stale') AND finished_utc IS NOT NULL AND error_code IS NOT NULL))
    ) STRICT""",
    """CREATE TABLE turns (
        session_id TEXT NOT NULL,
        input_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK(position > 0),
        assistant_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
        model_id TEXT NOT NULL CHECK(length(trim(model_id)) > 0),
        attempt_id TEXT NOT NULL UNIQUE,
        PRIMARY KEY(session_id, input_id),
        UNIQUE(session_id, position),
        FOREIGN KEY(session_id, input_id) REFERENCES requests(session_id, input_id),
        FOREIGN KEY(attempt_id, session_id, input_id) REFERENCES attempts(attempt_id, session_id, input_id)
    ) STRICT""",
)
