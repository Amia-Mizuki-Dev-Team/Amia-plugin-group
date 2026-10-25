"""Durable SQLite ledger for notices, subscriptions and delivery attempts.

Three independent facts are stored separately:

* a notice revision is ``draft``, ``published`` or ``withdrawn``;
* a published revision has a schedule (``publish_at`` / ``expires_at``);
* whether a group actually received it lives in ``delivery_attempts``.

The ledger is the only place a send is authorised from.  A claim is committed
before the provider is called, so a crash between claim and write-back leaves a
durable ``in_flight`` row that a restarted process must treat as ``unknown``
instead of sending again.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .logic import MAX_NOTICES, TERMINAL_DELIVERY_STATES, payload_digest

if TYPE_CHECKING:
    from collections.abc import Iterator

CRASH_REASON = "crash_after_claim"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notice_revisions (
    notice_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft', 'published', 'withdrawn')),
    source_scope TEXT NOT NULL,
    publish_at TEXT,
    expires_at TEXT,
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (notice_id, revision)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    scope TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('subscribed', 'unsubscribed')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    notice_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    owner_scope TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    target_scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('in_flight', 'accepted', 'failed', 'unknown')
    ),
    retry_safe INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    provider_code TEXT,
    message_id TEXT,
    payload_digest TEXT NOT NULL,
    manual_operation_id TEXT,
    boot_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (logical_key, attempt_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS delivery_attempts_single_active
    ON delivery_attempts (logical_key)
    WHERE status = 'in_flight';

CREATE UNIQUE INDEX IF NOT EXISTS delivery_attempts_manual_operation
    ON delivery_attempts (manual_operation_id)
    WHERE manual_operation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS delivery_attempts_notice
    ON delivery_attempts (notice_id, revision);
"""


class LedgerError(RuntimeError):
    """Base class for ledger failures the caller must handle."""


class LedgerUnreadableError(LedgerError):
    """The ledger file cannot be opened or migrated; delivery must stop."""


class NoticeMissingError(LedgerError):
    """The requested notice revision does not exist."""


class NoticeLimitReachedError(LedgerError):
    """The configured number of notices is already stored."""


class NoticeStateError(LedgerError):
    """The requested transition is not valid for the current notice state."""


@dataclass(frozen=True, slots=True)
class NoticeRecord:
    notice_id: str
    revision: int
    content: str
    state: str
    source_scope: str
    publish_at: str | None
    expires_at: str | None
    payload_digest: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: int
    logical_key: str
    attempt_no: int
    notice_id: str
    revision: int
    owner_scope: str
    source_scope: str
    target_scope: str
    status: str
    retry_safe: bool
    reason: str | None
    provider_code: str | None
    message_id: str | None
    payload_digest: str
    manual_operation_id: str | None
    boot_id: str
    claimed_at: str
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    granted: bool
    attempt: AttemptRecord | None
    status: str
    reason: str | None


def to_iso(moment: datetime | None = None) -> str:
    """Return a lexicographically sortable UTC timestamp."""

    value = moment if moment is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _notice(row: sqlite3.Row) -> NoticeRecord:
    return NoticeRecord(
        notice_id=row["notice_id"],
        revision=int(row["revision"]),
        content=row["content"],
        state=row["state"],
        source_scope=row["source_scope"],
        publish_at=row["publish_at"],
        expires_at=row["expires_at"],
        payload_digest=row["payload_digest"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _attempt(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=int(row["attempt_id"]),
        logical_key=row["logical_key"],
        attempt_no=int(row["attempt_no"]),
        notice_id=row["notice_id"],
        revision=int(row["revision"]),
        owner_scope=row["owner_scope"],
        source_scope=row["source_scope"],
        target_scope=row["target_scope"],
        status=row["status"],
        retry_safe=bool(row["retry_safe"]),
        reason=row["reason"],
        provider_code=row["provider_code"],
        message_id=row["message_id"],
        payload_digest=row["payload_digest"],
        manual_operation_id=row["manual_operation_id"],
        boot_id=row["boot_id"],
        claimed_at=row["claimed_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
    )


class NoticeLedger:
    """Small single-file ledger; every mutation is committed before returning."""

    def __init__(self, path: Path, *, boot_id: str | None = None) -> None:
        self.path = Path(path)
        self.boot_id = boot_id or uuid.uuid4().hex
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path), isolation_level=None, timeout=5.0
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
        except (OSError, sqlite3.DatabaseError) as exc:
            raise LedgerUnreadableError(type(exc).__name__) from exc

    # --- lifecycle -----------------------------------------------------
    def close(self) -> None:
        with suppress(sqlite3.Error):
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # --- notices -------------------------------------------------------
    def create_notice(
        self,
        content: str,
        *,
        source_scope: str,
        now: datetime | None = None,
        max_notices: int = MAX_NOTICES,
    ) -> NoticeRecord:
        timestamp = to_iso(now)
        with self._tx() as conn:
            count = conn.execute(
                "SELECT COUNT(DISTINCT notice_id) AS total FROM notice_revisions"
            ).fetchone()["total"]
            if count >= max_notices:
                raise NoticeLimitReachedError(str(count))
            notice_id = uuid.uuid4().hex[:12]
            conn.execute(
                """
                INSERT INTO notice_revisions (
                    notice_id, revision, content, state, source_scope,
                    publish_at, expires_at, payload_digest, created_at, updated_at
                ) VALUES (?, 1, ?, 'draft', ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    notice_id,
                    content,
                    source_scope,
                    payload_digest(content),
                    timestamp,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM notice_revisions WHERE notice_id = ? AND revision = 1",
                (notice_id,),
            ).fetchone()
        return _notice(row)

    def list_notices(self) -> list[NoticeRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM notice_revisions AS candidate
            WHERE candidate.revision = (
                SELECT MAX(later.revision) FROM notice_revisions AS later
                WHERE later.notice_id = candidate.notice_id
            )
            ORDER BY candidate.created_at, candidate.notice_id
            """
        ).fetchall()
        return [_notice(row) for row in rows]

    def get_notice(
        self, notice_id: str, revision: int | None = None
    ) -> NoticeRecord | None:
        if revision is None:
            row = self._conn.execute(
                """
                SELECT * FROM notice_revisions WHERE notice_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (notice_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM notice_revisions WHERE notice_id = ? AND revision = ?",
                (notice_id, revision),
            ).fetchone()
        return _notice(row) if row is not None else None

    def update_notice(
        self, notice_id: str, content: str, *, now: datetime | None = None
    ) -> NoticeRecord:
        """Edit the pending draft, or open a new draft revision.

        A published revision is immutable: revising an announcement must not
        change what a later delivery attempt would send under the same key.
        """

        timestamp = to_iso(now)
        with self._tx() as conn:
            latest = self._latest_row(conn, notice_id)
            if latest is None:
                raise NoticeMissingError(notice_id)
            if latest["state"] == "draft":
                conn.execute(
                    """
                    UPDATE notice_revisions
                    SET content = ?, payload_digest = ?, updated_at = ?
                    WHERE notice_id = ? AND revision = ?
                    """,
                    (
                        content,
                        payload_digest(content),
                        timestamp,
                        notice_id,
                        latest["revision"],
                    ),
                )
                target_revision = latest["revision"]
            else:
                target_revision = int(latest["revision"]) + 1
                conn.execute(
                    """
                    INSERT INTO notice_revisions (
                        notice_id, revision, content, state, source_scope,
                        publish_at, expires_at, payload_digest, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, 'draft', ?, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        notice_id,
                        target_revision,
                        content,
                        latest["source_scope"],
                        payload_digest(content),
                        timestamp,
                        timestamp,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM notice_revisions WHERE notice_id = ? AND revision = ?",
                (notice_id, target_revision),
            ).fetchone()
        return _notice(row)

    def delete_notice(self, notice_id: str) -> None:
        """Delete a notice that has not been published yet."""

        with self._tx() as conn:
            latest = self._latest_row(conn, notice_id)
            if latest is None:
                raise NoticeMissingError(notice_id)
            if latest["state"] != "draft":
                raise NoticeStateError(latest["state"])
            conn.execute(
                "DELETE FROM notice_revisions WHERE notice_id = ?", (notice_id,)
            )

    def publish(
        self,
        notice_id: str,
        *,
        source_scope: str,
        publish_at: datetime | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> NoticeRecord:
        timestamp = to_iso(now)
        publish_at_iso = to_iso(publish_at) if publish_at is not None else timestamp
        expires_at_iso = to_iso(expires_at) if expires_at is not None else None
        if expires_at_iso is not None and expires_at_iso <= publish_at_iso:
            raise NoticeStateError("expired_before_publish")
        with self._tx() as conn:
            latest = self._latest_row(conn, notice_id)
            if latest is None:
                raise NoticeMissingError(notice_id)
            if latest["state"] != "draft":
                raise NoticeStateError(latest["state"])
            conn.execute(
                """
                UPDATE notice_revisions
                SET state = 'published', source_scope = ?, publish_at = ?,
                    expires_at = ?, updated_at = ?
                WHERE notice_id = ? AND revision = ?
                """,
                (
                    source_scope,
                    publish_at_iso,
                    expires_at_iso,
                    timestamp,
                    notice_id,
                    latest["revision"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM notice_revisions WHERE notice_id = ? AND revision = ?",
                (notice_id, latest["revision"]),
            ).fetchone()
        return _notice(row)

    def withdraw(
        self, notice_id: str, *, now: datetime | None = None
    ) -> NoticeRecord:
        timestamp = to_iso(now)
        with self._tx() as conn:
            latest = self._latest_row(conn, notice_id)
            if latest is None:
                raise NoticeMissingError(notice_id)
            if latest["state"] != "published":
                raise NoticeStateError(latest["state"])
            conn.execute(
                """
                UPDATE notice_revisions
                SET state = 'withdrawn', updated_at = ?
                WHERE notice_id = ? AND revision = ?
                """,
                (timestamp, notice_id, latest["revision"]),
            )
            row = conn.execute(
                "SELECT * FROM notice_revisions WHERE notice_id = ? AND revision = ?",
                (notice_id, latest["revision"]),
            ).fetchone()
        return _notice(row)

    def due_notices(self, *, now: datetime | None = None) -> list[NoticeRecord]:
        """Return published revisions whose schedule window is open."""

        timestamp = to_iso(now)
        rows = self._conn.execute(
            """
            SELECT * FROM notice_revisions AS candidate
            WHERE candidate.state = 'published'
              AND candidate.revision = (
                  SELECT MAX(later.revision) FROM notice_revisions AS later
                  WHERE later.notice_id = candidate.notice_id
              )
              AND candidate.publish_at IS NOT NULL
              AND candidate.publish_at <= ?
              AND (candidate.expires_at IS NULL OR candidate.expires_at > ?)
            ORDER BY candidate.publish_at, candidate.notice_id
            """,
            (timestamp, timestamp),
        ).fetchall()
        return [_notice(row) for row in rows]

    @staticmethod
    def _latest_row(
        conn: sqlite3.Connection, notice_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM notice_revisions WHERE notice_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (notice_id,),
        ).fetchone()

    # --- subscriptions -------------------------------------------------
    def set_subscription(
        self, scope: str, *, subscribed: bool, now: datetime | None = None
    ) -> str:
        state = "subscribed" if subscribed else "unsubscribed"
        self._conn.execute(
            """
            INSERT INTO subscriptions (scope, state, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (scope, state, to_iso(now)),
        )
        return state

    def subscription_state(self, scope: str) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM subscriptions WHERE scope = ?", (scope,)
        ).fetchone()
        return row["state"] if row is not None else None

    def subscribed_scopes(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT scope FROM subscriptions WHERE state = 'subscribed' ORDER BY scope"
        ).fetchall()
        return [row["scope"] for row in rows]

    # --- delivery attempts ---------------------------------------------
    def claim(
        self,
        *,
        logical_key: str,
        notice_id: str,
        revision: int,
        owner_scope: str,
        source_scope: str,
        target_scope: str,
        payload: str,
        manual_operation_id: str | None = None,
        now: datetime | None = None,
    ) -> ClaimOutcome:
        """Persist one in-flight claim and commit it before returning.

        The partial unique index on ``logical_key WHERE status='in_flight'``
        makes the claim exclusive across workers and processes, not just
        across asyncio tasks.
        """

        timestamp = to_iso(now)
        digest = payload_digest(payload)
        with self._tx() as conn:
            if manual_operation_id is not None:
                replay = conn.execute(
                    "SELECT * FROM delivery_attempts WHERE manual_operation_id = ?",
                    (manual_operation_id,),
                ).fetchone()
                if replay is not None:
                    return ClaimOutcome(
                        granted=False,
                        attempt=_attempt(replay),
                        status=replay["status"],
                        reason="manual_replay",
                    )
            latest = conn.execute(
                """
                SELECT * FROM delivery_attempts WHERE logical_key = ?
                ORDER BY attempt_no DESC LIMIT 1
                """,
                (logical_key,),
            ).fetchone()
            if latest is not None and latest["payload_digest"] != digest:
                return ClaimOutcome(
                    granted=False,
                    attempt=_attempt(latest),
                    status=latest["status"],
                    reason="payload_conflict",
                )
            if latest is not None and latest["status"] == "in_flight":
                return ClaimOutcome(
                    granted=False,
                    attempt=_attempt(latest),
                    status="in_flight",
                    reason="in_flight",
                )
            attempt_no = int(latest["attempt_no"]) + 1 if latest is not None else 0
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO delivery_attempts (
                        logical_key, attempt_no, notice_id, revision,
                        owner_scope, source_scope, target_scope, status,
                        retry_safe, reason, provider_code, message_id,
                        payload_digest, manual_operation_id, boot_id,
                        claimed_at, finished_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'in_flight', 0, NULL, NULL,
                              NULL, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        logical_key,
                        attempt_no,
                        notice_id,
                        revision,
                        owner_scope,
                        source_scope,
                        target_scope,
                        digest,
                        manual_operation_id,
                        self.boot_id,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                # Another worker won the active-claim index between our read
                # and insert.  SQLite aborts the statement, so the transaction
                # can still be committed unchanged.
                current = conn.execute(
                    """
                    SELECT * FROM delivery_attempts WHERE logical_key = ?
                    ORDER BY attempt_no DESC LIMIT 1
                    """,
                    (logical_key,),
                ).fetchone()
                return ClaimOutcome(
                    granted=False,
                    attempt=_attempt(current) if current is not None else None,
                    status=current["status"] if current is not None else "in_flight",
                    reason="in_flight",
                )
            inserted = conn.execute(
                "SELECT * FROM delivery_attempts WHERE attempt_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return ClaimOutcome(
            granted=True, attempt=_attempt(inserted), status="in_flight", reason=None
        )

    def finish(
        self,
        attempt_id: int,
        status: str,
        *,
        retry_safe: bool = False,
        reason: str | None = None,
        provider_code: str | None = None,
        message_id: str | None = None,
        now: datetime | None = None,
    ) -> AttemptRecord | None:
        """Write one terminal status for an in-flight attempt.

        ``None`` means the write did not apply (missing row or already
        terminal).  The caller must then report ``unknown``: the durable claim
        still blocks an automatic resend.
        """

        if status not in TERMINAL_DELIVERY_STATES:
            raise ValueError(f"not a terminal status: {status}")
        timestamp = to_iso(now)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None or row["status"] != "in_flight":
                return None
            conn.execute(
                """
                UPDATE delivery_attempts
                SET status = ?, retry_safe = ?, reason = ?, provider_code = ?,
                    message_id = ?, finished_at = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'in_flight'
                """,
                (
                    status,
                    int(retry_safe),
                    reason,
                    provider_code,
                    message_id,
                    timestamp,
                    timestamp,
                    attempt_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM delivery_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _attempt(updated) if updated is not None else None

    def recover_in_flight(self, *, now: datetime | None = None) -> list[AttemptRecord]:
        """Mark claims owned by a previous process run as ``unknown``.

        A claim from an earlier boot might have been sent already; there is no
        evidence either way, so it is never retried automatically.  Claims
        owned by the current process are left alone: they belong to a worker
        that is still running (or to a task that crashed and must stay
        blocked, which is the conservative side).
        """

        timestamp = to_iso(now)
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_attempts
                WHERE status = 'in_flight' AND boot_id <> ?
                """,
                (self.boot_id,),
            ).fetchall()
            attempt_ids = [row["attempt_id"] for row in rows]
            if attempt_ids:
                placeholders = ",".join("?" * len(attempt_ids))
                conn.execute(
                    f"""
                    UPDATE delivery_attempts
                    SET status = 'unknown', retry_safe = 0,
                        reason = COALESCE(reason, ?), finished_at = ?,
                        updated_at = ?
                    WHERE attempt_id IN ({placeholders})
                    """,
                    (CRASH_REASON, timestamp, timestamp, *attempt_ids),
                )
                recovered = conn.execute(
                    f"""
                    SELECT * FROM delivery_attempts
                    WHERE attempt_id IN ({placeholders})
                    ORDER BY attempt_id
                    """,
                    attempt_ids,
                ).fetchall()
            else:
                recovered = []
        return [_attempt(row) for row in recovered]

    def attempt_by_id(self, attempt_id: int) -> AttemptRecord | None:
        row = self._conn.execute(
            "SELECT * FROM delivery_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return _attempt(row) if row is not None else None

    def latest_attempt(self, logical_key: str) -> AttemptRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM delivery_attempts WHERE logical_key = ?
            ORDER BY attempt_no DESC LIMIT 1
            """,
            (logical_key,),
        ).fetchone()
        return _attempt(row) if row is not None else None

    def attempts(self, logical_key: str) -> list[AttemptRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM delivery_attempts WHERE logical_key = ?
            ORDER BY attempt_no
            """,
            (logical_key,),
        ).fetchall()
        return [_attempt(row) for row in rows]

    def attempts_for_notice(
        self, notice_id: str, revision: int
    ) -> list[AttemptRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM delivery_attempts
            WHERE notice_id = ? AND revision = ? ORDER BY target_scope, attempt_no
            """,
            (notice_id, revision),
        ).fetchall()
        return [_attempt(row) for row in rows]

    def accepted_scopes(self, notice_id: str, revision: int) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT target_scope FROM delivery_attempts
            WHERE notice_id = ? AND revision = ? AND status = 'accepted'
            ORDER BY target_scope
            """,
            (notice_id, revision),
        ).fetchall()
        return [row["target_scope"] for row in rows]
