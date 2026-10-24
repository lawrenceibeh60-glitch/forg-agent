"""Persistent task checkpoints for Forge's agent loop.

SQLite gives atomic, restart-safe checkpoints on persistent storage. Set TASK_DB_PATH
to a mounted persistent path in production; the default data directory may be ephemeral
on hosting plans without a persistent disk.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

_DB = Path(os.getenv("TASK_DB_PATH", "data/tasks.sqlite3"))
_LOCK = threading.RLock()


def _connect():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_DB), timeout=10)
    db.row_factory = sqlite3.Row
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
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS tasks_session_updated ON tasks(session_id, updated_at DESC)")
    db.commit()
    return db


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
    with _LOCK, _connect() as db:
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
        elif key == "status" and value not in {"running", "waiting_approval", "completed", "blocked", "failed", "cancelled"}:
            raise ValueError("invalid task status")
        sets.append(column + "=?")
        vals.append(value)
    if not sets:
        return get(task_id)
    sets.append("updated_at=?")
    vals.extend([time.time(), task_id])
    with _LOCK, _connect() as db:
        db.execute("UPDATE tasks SET " + ", ".join(sets) + " WHERE id=?", vals)
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _decode(row)


def get(task_id):
    with _LOCK, _connect() as db:
        return _decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def latest_for_session(session_id):
    with _LOCK, _connect() as db:
        return _decode(db.execute(
            "SELECT * FROM tasks WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
            (str(session_id),),
        ).fetchone())


def list_for_session(session_id, limit=10):
    with _LOCK, _connect() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE session_id=? ORDER BY updated_at DESC LIMIT ?",
            (str(session_id), max(1, min(int(limit), 50))),
        ).fetchall()
    return [_decode(row) for row in rows]


def list_recent(limit=30):
    with _LOCK, _connect() as db:
        rows = db.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_decode(row) for row in rows]


def health():
    persistent_hint = str(_DB.resolve())
    try:
        with _LOCK, _connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"ok": True, "backend": "sqlite", "path": persistent_hint,
                "durability_note": "Survives process restarts; survives redeploys only when TASK_DB_PATH is on persistent storage."}
    except Exception as exc:
        return {"ok": False, "backend": "sqlite", "error": type(exc).__name__}
