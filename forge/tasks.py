"""Durable task and conversation storage.

PostgreSQL (DATABASE_URL) is the shared, cross-instance backend in production.
SQLite remains the zero-configuration local/test fallback. Vercel's filesystem is
ephemeral, so SQLite there is not durable across redeployments.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
import uuid
from pathlib import Path

_DEFAULT_DB = Path(os.getenv("TASK_DB_PATH", "data/tasks.sqlite3"))
_DB = _DEFAULT_DB
_LOCK = threading.RLock()


def _backend():
    mode = os.getenv("TASK_DB_BACKEND", "").strip().lower()
    if mode == "sqlite":
        return "sqlite"
    if mode == "postgres":
        return "postgres"
    if os.getenv("DATABASE_URL", "").strip() and not os.getenv("TASK_DB_PATH", "").strip():
        return "postgres"
    return "sqlite"


class _Connection:
    def __init__(self, raw, backend):
        self.raw = raw
        self.backend = backend

    def execute(self, sql, params=()):
        if self.backend == "postgres":
            sql = sql.replace("?", "%s")
            return self.raw.execute(sql, params)
        return self.raw.execute(sql, params)

    def commit(self):
        return self.raw.commit()

    def close(self):
        return self.raw.close()


def _connect():
    backend = _backend()
    if backend == "postgres":
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("TASK_DB_BACKEND=postgres requires DATABASE_URL")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL storage requires psycopg[binary]") from exc
        # Render may provide postgres://; psycopg accepts postgresql:// consistently.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        raw = psycopg.connect(url, connect_timeout=10, row_factory=dict_row)
        db = _Connection(raw, backend)
    else:
        _DB.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(_DB), timeout=10)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA busy_timeout=10000")
        db = _Connection(raw, backend)

    db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '[]',
            completed_json TEXT NOT NULL DEFAULT '[]',
            current_step TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS tasks_session_updated ON tasks(session_id, updated_at DESC)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_id TEXT PRIMARY KEY,
            messages_json TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )
    """)
    db.commit()
    return db


@contextmanager
def _database():
    db = _connect()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _decode(row):
    if row is None:
        return None
    d = dict(row)
    for key in ("plan_json", "completed_json", "checkpoint_json"):
        try:
            d[key[:-5] if key.endswith("_json") else key] = json.loads(d.pop(key))
        except (TypeError, ValueError):
            d[key] = [] if key in ("plan_json", "completed_json") else {}
    return d


def create(session_id, goal):
    now = time.time()
    task_id = uuid.uuid4().hex
    with _LOCK, _database() as db:
        db.execute(
            "INSERT INTO tasks(id,session_id,goal,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, str(session_id), str(goal)[:12000], "running", now, now),
        )
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _decode(row)


def update(task_id, **fields):
    allowed = {"goal", "status", "plan", "completed", "current_step", "last_error", "checkpoint"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("unsupported task fields: " + ", ".join(sorted(unknown)))
    sets, vals = [], []
    for key, value in fields.items():
        column = {"plan": "plan_json", "completed": "completed_json", "checkpoint": "checkpoint_json"}.get(key, key)
        if key in ("plan", "completed", "checkpoint"):
            value = json.dumps(value, ensure_ascii=False)[:30000]
        elif key in ("goal", "current_step", "last_error"):
            value = str(value)[:12000]
        elif key == "status" and value not in {"running", "waiting_approval", "completed", "paused", "blocked", "failed", "cancelled"}:
            raise ValueError("invalid task status")
        sets.append(column + "=?")
        vals.append(value)
    if not sets:
        return get(task_id)
    sets.append("updated_at=?")
    vals.extend([time.time(), task_id])
    with _LOCK, _database() as db:
        db.execute("UPDATE tasks SET " + ", ".join(sets) + " WHERE id=?", vals)
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _decode(row)


def get(task_id):
    with _LOCK, _database() as db:
        return _decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def latest_for_session(session_id):
    with _LOCK, _database() as db:
        return _decode(db.execute(
            "SELECT * FROM tasks WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
            (str(session_id),),
        ).fetchone())


def list_for_session(session_id, limit=10):
    with _LOCK, _database() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE session_id=? ORDER BY updated_at DESC LIMIT ?",
            (str(session_id), max(1, min(int(limit), 50))),
        ).fetchall()
    return [_decode(row) for row in rows]


def list_recent(limit=30):
    with _LOCK, _database() as db:
        rows = db.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_decode(row) for row in rows]


def save_session(session_id, messages):
    """Atomically persist a full chat transcript for subsequent requests/instances."""
    payload = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if len(payload) > 8_000_000:
        raise ValueError("conversation is too large to persist")
    now = time.time()
    with _LOCK, _database() as db:
        db.execute(
            "INSERT INTO conversations(session_id,messages_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET messages_json=excluded.messages_json, updated_at=excluded.updated_at",
            (str(session_id), payload, now),
        )


def load_session(session_id):
    with _LOCK, _database() as db:
        row = db.execute(
            "SELECT messages_json FROM conversations WHERE session_id=?",
            (str(session_id),),
        ).fetchone()
    if not row:
        return None
    try:
        messages = json.loads(row["messages_json"])
        return messages if isinstance(messages, list) else None
    except (TypeError, ValueError):
        return None


def recover_interrupted(stale_after=0):
    """Mark stale active tasks resumable; avoid stealing work from a live instance."""
    now = time.time()
    cutoff = now - max(0, float(stale_after))
    recovered = 0
    with _LOCK, _database() as db:
        rows = db.execute(
            "SELECT id, status, checkpoint_json, updated_at FROM tasks "
            "WHERE status IN ('running', 'waiting_approval') AND updated_at<?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            try:
                checkpoint = json.loads(row["checkpoint_json"] or "{}")
            except (TypeError, ValueError):
                checkpoint = {}
            checkpoint.update({"phase": "interrupted", "previous_status": row["status"],
                               "recovered_at": now, "stale_seconds": int(now - float(row["updated_at"]))})
            db.execute(
                "UPDATE tasks SET status='blocked', current_step=?, last_error=?, checkpoint_json=?, updated_at=? WHERE id=?",
                ("Process restart or execution timeout; checkpoint is resumable.",
                 "The task stopped updating before reaching a final state.",
                 json.dumps(checkpoint, ensure_ascii=False), now, row["id"]),
            )
            recovered += 1
    return recovered


def health():
    backend = _backend()
    try:
        with _LOCK, _database() as db:
            db.execute("SELECT 1").fetchone()
        if backend == "postgres":
            return {"ok": True, "backend": "postgres", "path": "DATABASE_URL",
                    "durability_note": "Shared database survives application instance changes and redeployments; availability depends on the configured PostgreSQL service."}
        persistent_hint = str(_DB.resolve())
        return {"ok": True, "backend": "sqlite", "path": persistent_hint,
                "durability_note": "Survives process restarts; survives redeploys only when TASK_DB_PATH is on persistent storage."}
    except Exception as exc:
        return {"ok": False, "backend": backend, "error": type(exc).__name__}
