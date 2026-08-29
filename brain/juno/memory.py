"""Everything Juno remembers, in one SQLite file.

Three stores with different lifetimes:

``conversation``  what was said, both directions. Trimmed by recency.
``timeline``      observations from every device — screen focus, app usage,
                  presence, notifications. This is what makes "what have I been
                  doing?" answerable, and it is deliberately *text*, never
                  pixels, so a day of it costs almost nothing to send to a model.
``facts``         durable things worth keeping: preferences, ongoing projects,
                  who people are.

Retrieval over facts uses SQLite's built-in FTS5 rather than vector search.
That is a deliberate trade: FTS5 needs no native extension (which matters on
the free ARM box), no embedding model, and no per-write inference, and at the
scale of one person's facts — thousands, not millions — keyword-plus-recency
retrieves about as well. ``search_facts`` is the only seam that would change if
that ever stops being true.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation (
    id        INTEGER PRIMARY KEY,
    ts        REAL    NOT NULL,
    role      TEXT    NOT NULL CHECK (role IN ('user', 'juno')),
    text      TEXT    NOT NULL,
    device_id TEXT
);
CREATE INDEX IF NOT EXISTS conversation_ts ON conversation (ts DESC);

CREATE TABLE IF NOT EXISTS timeline (
    id        INTEGER PRIMARY KEY,
    ts        REAL    NOT NULL,
    kind      TEXT    NOT NULL,
    device_id TEXT,
    summary   TEXT    NOT NULL,
    data      TEXT
);
CREATE INDEX IF NOT EXISTS timeline_ts   ON timeline (ts DESC);
CREATE INDEX IF NOT EXISTS timeline_kind ON timeline (kind, ts DESC);

CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    subject TEXT NOT NULL,
    body    TEXT NOT NULL,
    source  TEXT,
    UNIQUE (subject)
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(subject, body, content='facts', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts (rowid, subject, body) VALUES (new.id, new.subject, new.body);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts (facts_fts, rowid, subject, body)
        VALUES ('delete', old.id, old.subject, old.body);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts (facts_fts, rowid, subject, body)
        VALUES ('delete', old.id, old.subject, old.body);
    INSERT INTO facts_fts (rowid, subject, body) VALUES (new.id, new.subject, new.body);
END;

CREATE TABLE IF NOT EXISTS spend (
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    model   TEXT NOT NULL,
    usd     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_ts ON spend (ts DESC);
"""


@dataclass(slots=True)
class TimelineRow:
    ts: float
    kind: str
    summary: str
    device_id: str | None = None
    data: dict[str, Any] | None = None


class Memory:
    """Synchronous on purpose.

    Every call here is a single indexed SQLite statement on a local file —
    microseconds. Wrapping that in async machinery would add more overhead than
    it saves, so the orchestrator calls straight into it. The lock exists only
    because the WebSocket hub and the scheduler run on different threads.
    """

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # WAL lets the scheduler read while a device is mid-write.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- conversation --------------------------------------------------------

    def add_turn(self, role: str, text: str, device_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO conversation (ts, role, text, device_id) VALUES (?, ?, ?, ?)",
                (time.time(), role, text, device_id),
            )
            self._db.commit()

    def recent_turns(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, role, text, device_id FROM conversation ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return list(reversed(rows))

    # --- timeline ------------------------------------------------------------

    def add_event(self, row: TimelineRow) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO timeline (ts, kind, device_id, summary, data) VALUES (?, ?, ?, ?, ?)",
                (
                    row.ts,
                    row.kind,
                    row.device_id,
                    row.summary,
                    json.dumps(row.data) if row.data else None,
                ),
            )
            self._db.commit()

    def events_since(self, since: float, kinds: Iterable[str] | None = None) -> list[sqlite3.Row]:
        query = "SELECT ts, kind, device_id, summary FROM timeline WHERE ts >= ?"
        params: list[Any] = [since]
        if kinds:
            kinds = list(kinds)
            query += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        query += " ORDER BY ts ASC"
        with self._lock:
            return self._db.execute(query, params).fetchall()

    def digest(self, since: float, limit: int = 80) -> str:
        """The timeline as compact text for a prompt.

        Consecutive rows with the same summary collapse into one line with a
        duration, which is what turns 200 ten-second screen samples into
        "Firefox — reddit.com  (34m)". Without this the timeline would dominate
        the context window and cost real money.
        """
        rows = self.events_since(since)
        if not rows:
            return "(nothing recorded)"

        lines: list[str] = []
        run_start = rows[0]["ts"]
        run_summary = rows[0]["summary"]

        def flush(end_ts: float) -> None:
            minutes = max(1, round((end_ts - run_start) / 60))
            stamp = time.strftime("%H:%M", time.localtime(run_start))
            lines.append(f"{stamp}  {run_summary}  ({minutes}m)")

        for row in rows[1:]:
            if row["summary"] != run_summary:
                flush(row["ts"])
                run_start, run_summary = row["ts"], row["summary"]
        flush(rows[-1]["ts"])

        if len(lines) > limit:
            lines = ["… earlier activity trimmed …", *lines[-limit:]]
        return "\n".join(lines)

    # --- facts ---------------------------------------------------------------

    def remember(self, subject: str, body: str, source: str | None = None) -> None:
        """Upsert. A fact's subject is its identity, so restating something
        updates it instead of accumulating contradictory copies."""
        with self._lock:
            self._db.execute(
                """
                INSERT INTO facts (ts, subject, body, source) VALUES (?, ?, ?, ?)
                ON CONFLICT (subject) DO UPDATE SET body = excluded.body, ts = excluded.ts
                """,
                (time.time(), subject, body, source),
            )
            self._db.commit()

    def forget(self, subject: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts WHERE subject = ?", (subject,))
            self._db.commit()
            return cur.rowcount > 0

    def search_facts(self, query: str, limit: int = 8) -> list[sqlite3.Row]:
        # FTS5 treats plenty of punctuation as syntax, so a raw user phrase can
        # be a syntax error rather than a search. Quoting each word makes any
        # input a safe literal OR-query.
        terms = " OR ".join(f'"{w}"' for w in query.split() if w.strip())
        if not terms:
            return []
        with self._lock:
            try:
                return self._db.execute(
                    """
                    SELECT f.subject, f.body, f.ts FROM facts_fts
                    JOIN facts f ON f.id = facts_fts.rowid
                    WHERE facts_fts MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (terms, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []

    def all_facts(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT subject, body, ts FROM facts ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    # --- spend ---------------------------------------------------------------

    def record_spend(self, model: str, usd: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO spend (ts, model, usd) VALUES (?, ?, ?)",
                (time.time(), model, usd),
            )
            self._db.commit()

    def spend_since(self, since: float) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(usd), 0.0) AS total FROM spend WHERE ts >= ?", (since,)
            ).fetchone()
        return float(row["total"])
