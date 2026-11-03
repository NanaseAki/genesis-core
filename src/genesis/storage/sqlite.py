"""Small host-owned SQLite ledger. Model generation never runs in a transaction.

An attempt is not a durable execution lease. Retrying the same request fences
earlier running attempts as unknown. Concurrent different inputs are optimistic:
only a result using the current session revision can commit. No exactly-once
model execution, encryption, migration or backup claim is made.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Literal
from uuid import UUID, uuid4

from genesis.core import (
    DuplicateInputError, PendingAttempt, SessionExistsError, SessionNotFoundError,
    SessionSnapshot, Turn, UserInput,
)
from genesis.data_paths import data_root_path
from genesis.day0 import make_day0, utc_now, verify_day0
from genesis.identity import CompanionIdentity
from genesis.memory import EvidenceOrigin, EvidenceReference, TrustedEvidenceRegistry
from genesis.models.contracts import Message, Role, validate_model_id
from genesis.storage.schema import APPLICATION_ID, DDL, SCHEMA_VERSION
from genesis.text import TextContractError, validate_text


class SchemaVersionError(ValueError):
    pass


class DataIntegrityError(ValueError):
    pass


class StaleAttemptError(RuntimeError):
    """Attempt was superseded, or its context is no longer the current revision."""


class TransactionNestingError(RuntimeError):
    """A transaction entry cannot adopt or roll back another entry's work."""


@dataclass(frozen=True)
class LedgerEvent:
    sequence: int
    reference: EvidenceReference
    parent_event_id: UUID | None
    kind: str
    content: str
    recorded_utc: str


@dataclass(frozen=True)
class AttemptStatus:
    attempt_id: UUID
    state: str
    error_code: str | None


class SQLiteStore:
    """One companion/database, synchronous connection owned by one thread.

    Only synthetic development datasets are initialized in M2-A. Instantiation
    is explicit; package import and the original console demo do not open it.
    sqlite3.Error and validation errors propagate; existing data is never reset.
    """

    def __init__(self, data_root: str | Path | None = None) -> None:
        if sqlite3.sqlite_version_info < (3, 37, 0):
            raise RuntimeError("SQLite 3.37+ is required for the v1 STRICT schema")
        root = data_root_path(data_root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "genesis-synthetic.sqlite3"
        if self.path.resolve().parent != root or self.path.is_symlink():
            raise ValueError("database must be directly inside the selected data root")
        # Exclusive creation distinguishes a genuinely new file from a damaged,
        # empty or unrelated DB. Never initialize over an existing file.
        try:
            with self.path.open("xb"):
                pass
            new = True
        except FileExistsError:
            new = False
        self._connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                          isolation_level=None, timeout=5)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise DataIntegrityError("SQLite foreign keys are unavailable")
            self._connection.execute("PRAGMA synchronous = FULL")
            if new:
                self._initialize()
            self._validate()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _commit(self) -> None:
        self._connection.commit()  # The durable publication point.

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connection
        # The connection belongs to one thread. Starting from no transaction
        # establishes ownership before BEGIN; a nested rejection MUST NOT roll
        # back the caller's existing transaction. BEGIN itself can be interrupted
        # after SQLite accepts it, so it belongs inside the cleanup protection.
        if connection.in_transaction:
            raise TransactionNestingError("nested transactions are not supported")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            self._commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    def _initialize(self) -> None:
        companion_id, event_id, created = uuid4(), uuid4(), utc_now()
        text, digest = make_day0(companion_id, created)
        with self._transaction() as db:
            for statement in DDL:
                db.execute(statement)
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("INSERT INTO companion VALUES (1,?,?,?,?)",
                       (str(companion_id), created, str(event_id), digest))
            self._event(db, event_id, None, "day0", "host_initialization", event_id, None, text, created)

    @staticmethod
    def _normalized(sql: str) -> str:
        return re.sub(r"\s+", " ", sql).strip().rstrip(";").casefold()

    def _validate(self) -> None:
        with self._transaction(write=False) as db:
            if (db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                    or db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
                raise SchemaVersionError("unsupported or uninitialized database; no automatic rebuild")
            if db.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
                raise DataIntegrityError("SQLite integrity check failed")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise DataIntegrityError("SQLite foreign key check failed")
            actual = {self._normalized(r[0]) for r in db.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )}
            if actual != {self._normalized(sql) for sql in DDL}:
                raise DataIntegrityError("schema does not match the supported v1 contract")
            rows = db.execute("SELECT c.*, e.content, e.kind, e.recorded_utc FROM companion c "
                              "JOIN events e ON e.event_id=c.day0_event_id").fetchall()
            if len(rows) != 1 or rows[0]["kind"] != "day0":
                raise DataIntegrityError("exactly one Day 0 identity is required")
            row = rows[0]
            self._identity = CompanionIdentity(UUID(row["companion_id"]))
            verify_day0(row["content"], row["day0_sha256"], self._identity.companion_id,
                        row["created_utc"])
            if row["recorded_utc"] != row["created_utc"]:
                raise DataIntegrityError("Day 0 timestamps disagree")
            # Structural constraints plus cross-table ledger invariants. Receipt
            # and history are the SAME turns row, never independently updated.
            invalid = db.execute("""SELECT 1 FROM sessions s WHERE s.revision !=
                (SELECT count(*) FROM turns t WHERE t.session_id=s.session_id)
                OR s.revision != COALESCE((SELECT max(position) FROM turns t
                    WHERE t.session_id=s.session_id), 0)
                UNION ALL
                SELECT 1 FROM requests q JOIN events e ON e.event_id=q.user_event_id
                WHERE e.kind!='user_input' OR e.session_id!=q.session_id
                UNION ALL
                SELECT 1 FROM turns t JOIN events e ON e.event_id=t.assistant_event_id
                JOIN attempts a ON a.attempt_id=t.attempt_id
                WHERE e.kind!='model_reply' OR e.session_id!=t.session_id
                   OR a.state!='committed' OR a.base_revision!=t.position-1
                UNION ALL
                SELECT 1 FROM attempts a WHERE a.state='committed'
                  AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.attempt_id=a.attempt_id)
                UNION ALL
                SELECT 1 FROM events e WHERE
                  (e.kind='user_input' AND NOT EXISTS (SELECT 1 FROM requests q WHERE q.user_event_id=e.event_id))
                  OR (e.kind='model_reply' AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.assistant_event_id=e.event_id))
                UNION ALL
                SELECT 1 FROM events e JOIN events p ON p.event_id=e.parent_event_id
                JOIN events r ON r.event_id=e.root_event_id
                WHERE e.root_event_id!=p.root_event_id OR e.session_id!=p.session_id
                    OR e.session_id!=r.session_id OR r.event_id!=r.root_event_id
                LIMIT 1""").fetchone()
            if invalid:
                raise DataIntegrityError("ledger relationships are inconsistent")
            # Keep the physical v1 schema unchanged. Older out-of-contract text
            # is rejected with its file preserved, never silently normalized.
            try:
                for value in db.execute("SELECT content FROM events UNION ALL SELECT model_id FROM turns"):
                    validate_text(value[0], field="persisted text")
            except TextContractError as error:
                raise DataIntegrityError("persisted text violates the text contract") from error

    @property
    def identity(self) -> CompanionIdentity:
        return self._identity

    def day0(self) -> tuple[str, str]:
        row = self._connection.execute("SELECT e.content,c.day0_sha256 FROM companion c "
                                       "JOIN events e ON e.event_id=c.day0_event_id").fetchone()
        return row[0], row[1]

    @staticmethod
    def _require_session(db: sqlite3.Connection, session_id: UUID) -> int:
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID")
        row = db.execute("SELECT revision FROM sessions WHERE session_id=?", (str(session_id),)).fetchone()
        if row is None:
            raise SessionNotFoundError("session does not exist")
        return row[0]

    @staticmethod
    def _turn(row: sqlite3.Row) -> Turn:
        return Turn(UserInput(UUID(row["input_id"]), row["user_content"]),
                    Message(Role.ASSISTANT, row["assistant_content"]), row["model_id"])

    @staticmethod
    def _turn_rows(db: sqlite3.Connection, session_id: UUID) -> list[sqlite3.Row]:
        return db.execute("""SELECT t.*, u.content AS user_content, a.content AS assistant_content
            FROM turns t JOIN requests q USING(session_id,input_id)
            JOIN events u ON u.event_id=q.user_event_id
            JOIN events a ON a.event_id=t.assistant_event_id
            WHERE t.session_id=? ORDER BY t.position""", (str(session_id),)).fetchall()

    def _snapshot(self, db: sqlite3.Connection, session_id: UUID) -> SessionSnapshot:
        self._require_session(db, session_id)
        return SessionSnapshot(session_id, self.identity.companion_id,
                               tuple(self._turn(r) for r in self._turn_rows(db, session_id)))

    def create_session(self, session_id: UUID) -> SessionSnapshot:
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM sessions WHERE session_id=?", (str(session_id),)).fetchone():
                raise SessionExistsError("session already exists")
            db.execute("INSERT INTO sessions(session_id,created_utc) VALUES (?,?)", (str(session_id), utc_now()))
            return self._snapshot(db, session_id)

    def get_session(self, session_id: UUID) -> SessionSnapshot:
        with self._transaction(write=False) as db:
            return self._snapshot(db, session_id)

    def session_ids(self) -> tuple[UUID, ...]:
        """Discover existing sessions without needing an in-memory ID list."""
        return tuple(UUID(row[0]) for row in self._connection.execute(
            "SELECT session_id FROM sessions ORDER BY rowid"))

    def _lookup(self, db: sqlite3.Connection, session_id: UUID, user_input: UserInput) -> Turn | None:
        self._require_session(db, session_id)
        row = db.execute("SELECT e.content FROM requests q JOIN events e ON e.event_id=q.user_event_id "
                         "WHERE q.session_id=? AND q.input_id=?",
                         (str(session_id), str(user_input.input_id))).fetchone()
        if row is not None and row[0] != user_input.content:
            raise DuplicateInputError("input ID already has different persisted content")
        completed = db.execute("""SELECT t.*, u.content AS user_content, a.content AS assistant_content
            FROM turns t JOIN requests q USING(session_id,input_id)
            JOIN events u ON u.event_id=q.user_event_id
            JOIN events a ON a.event_id=t.assistant_event_id
            WHERE t.session_id=? AND t.input_id=?""",
                               (str(session_id), str(user_input.input_id))).fetchone()
        return self._turn(completed) if completed is not None else None

    def lookup(self, session_id: UUID, user_input: UserInput) -> Turn | None:
        with self._transaction(write=False) as db:
            return self._lookup(db, session_id, user_input)

    @staticmethod
    def _event(
        db: sqlite3.Connection, event_id: UUID, session_id: UUID | None,
        kind: str, origin: str, root: UUID, parent: UUID | None,
        content: str, recorded: str | None = None,
    ) -> None:
        validate_text(content, field="event content")
        db.execute("""INSERT INTO events(event_id,session_id,kind,origin,root_event_id,
            parent_event_id,content,recorded_utc) VALUES (?,?,?,?,?,?,?,?)""",
                   (str(event_id), None if session_id is None else str(session_id), kind, origin,
                    str(root), None if parent is None else str(parent), content, recorded or utc_now()))

    def begin_attempt(self, session_id: UUID, user_input: UserInput) -> PendingAttempt | Turn:
        with self._transaction() as db:
            previous = self._lookup(db, session_id, user_input)
            if previous is not None:
                return previous
            sid, iid, now = str(session_id), str(user_input.input_id), utc_now()
            if not db.execute("SELECT 1 FROM requests WHERE session_id=? AND input_id=?", (sid, iid)).fetchone():
                event_id = uuid4()
                self._event(db, event_id, session_id, "user_input", "user_input", event_id, None, user_input.content)
                db.execute("INSERT INTO requests VALUES (?,?,?)", (sid, iid, str(event_id)))
            # Retrying explicitly fences any old process for THIS input, without
            # falsely claiming it crashed or that model execution did not occur.
            db.execute("UPDATE attempts SET state='unknown',error_code='retry_superseded',finished_utc=? "
                       "WHERE session_id=? AND input_id=? AND state='running'", (now, sid, iid))
            attempt_id = uuid4()
            revision = self._require_session(db, session_id)
            snapshot = self._snapshot(db, session_id)
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,'running',NULL,?,NULL)",
                       (str(attempt_id), sid, iid, revision, now))
            return PendingAttempt(attempt_id, snapshot, user_input)

    def fail_attempt(self, attempt_id: UUID, state: Literal["failed", "interrupted"], code: str) -> None:
        if state not in ("failed", "interrupted"):
            raise ValueError("invalid failure state")
        with self._transaction() as db:
            db.execute("UPDATE attempts SET state=?,error_code=?,finished_utc=? "
                       "WHERE attempt_id=? AND state='running'", (state, code, utc_now(), str(attempt_id)))

    def commit_turn(self, attempt: PendingAttempt, turn: Turn) -> Turn:
        validate_model_id(turn.model_id)
        validate_text(turn.user_input.content, field="input content")
        validate_text(turn.assistant.content, field="reply content")
        if turn.user_input != attempt.user_input or turn.assistant.role is not Role.ASSISTANT:
            raise ValueError("turn does not match attempt")
        stale = False
        with self._transaction() as db:
            sid, iid = str(attempt.snapshot.session_id), str(attempt.user_input.input_id)
            row = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (str(attempt.attempt_id),)).fetchone()
            if row is None or (row["session_id"], row["input_id"]) != (sid, iid):
                raise ValueError("unknown or mismatched attempt")
            previous = self._lookup(db, attempt.snapshot.session_id, attempt.user_input)
            if previous is not None:
                return previous  # The already committed winner is authoritative.
            revision = self._require_session(db, attempt.snapshot.session_id)
            if row["state"] != "running" or row["base_revision"] != revision:
                if row["state"] == "running":
                    db.execute("UPDATE attempts SET state='stale',error_code='stale_context',finished_utc=? "
                               "WHERE attempt_id=?", (utc_now(), str(attempt.attempt_id)))
                stale = True
            else:
                assistant_id = uuid4()
                self._event(db, assistant_id, attempt.snapshot.session_id, "model_reply", "model_output",
                            assistant_id, None, turn.assistant.content)
                db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?)", (sid, iid, revision + 1,
                           str(assistant_id), turn.model_id, str(attempt.attempt_id)))
                db.execute("UPDATE attempts SET state='committed',finished_utc=? WHERE attempt_id=?",
                           (utc_now(), str(attempt.attempt_id)))
                db.execute("UPDATE sessions SET revision=revision+1 WHERE session_id=? AND revision=?",
                           (sid, revision))
                # History and receipt share this row; all writes publish at the
                # enclosing transaction's COMMIT. No in-memory cache to diverge.
        if stale:
            raise StaleAttemptError("context changed or attempt superseded; retry explicitly")
        return turn

    def attempts(self, session_id: UUID, input_id: UUID) -> tuple[AttemptStatus, ...]:
        return tuple(AttemptStatus(UUID(r[0]), r[1], r[2]) for r in self._connection.execute(
            "SELECT attempt_id,state,error_code FROM attempts WHERE session_id=? AND input_id=? ORDER BY rowid",
            (str(session_id), str(input_id))))

    def events(self, session_id: UUID) -> tuple[LedgerEvent, ...]:
        with self._transaction(write=False) as db:
            self._require_session(db, session_id)
            return tuple(LedgerEvent(r["sequence"], EvidenceReference(UUID(r["event_id"]),
                         EvidenceOrigin(r["origin"]), UUID(r["root_event_id"])),
                         UUID(r["parent_event_id"]) if r["parent_event_id"] else None,
                         r["kind"], r["content"], r["recorded_utc"]) for r in db.execute(
                             "SELECT * FROM events WHERE session_id=? ORDER BY sequence", (str(session_id),)))

    def evidence_registry(self, session_id: UUID) -> TrustedEvidenceRegistry:
        return TrustedEvidenceRegistry(event.reference for event in self.events(session_id))

    def record_derived(
        self, session_id: UUID, source_event_id: UUID, *,
        kind: Literal["tool_echo", "summary", "inference"], content: str | None = None,
    ) -> EvidenceReference:
        """Trusted host ingestion ONLY. Root/origin are loaded, never model fields.

        Tool echoes copy the stored source exactly. Derived model summaries and
        inferences need explicit source selection by trusted host code, not an
        unvalidated model citation. No external tool root ingestion in M2-A.
        """
        if kind not in ("tool_echo", "summary", "inference"):
            raise ValueError("unsupported derivation kind")
        with self._transaction() as db:
            self._require_session(db, session_id)
            source = db.execute("SELECT * FROM events WHERE event_id=? AND session_id=?",
                                (str(source_event_id), str(session_id))).fetchone()
            if source is None:
                raise ValueError("source must be a stored event in this session")
            if kind == "tool_echo":
                if content is not None:
                    raise ValueError("echo content is copied from the source")
                content = source["content"]
            validate_text(content, field="derived content")
            origin = EvidenceOrigin.TOOL_RESULT if kind == "tool_echo" else EvidenceOrigin.MODEL_OUTPUT
            event_id, root = uuid4(), UUID(source["root_event_id"])
            self._event(db, event_id, session_id, kind, origin.value, root, source_event_id, content)
            return EvidenceReference(event_id, origin, root)
