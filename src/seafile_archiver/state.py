"""Protokoll in SQLite: wer wurde wann exportiert und gelöscht.

User-Status:
  exported     alle exportierbaren Bibliotheken erfolgreich exportiert
  failed       mindestens eine Bibliothek fehlgeschlagen, wird im nächsten Lauf erneut versucht
  reactivated  User wurde nach dem Export wieder aktiv, wird nicht gelöscht
  deleted      User und Bibliotheken in Seafile gelöscht

Bibliotheks-Status: exported | failed | encrypted
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email          TEXT PRIMARY KEY,
    uid            TEXT,
    contact_email  TEXT,
    archive_dir    TEXT,
    status         TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    exported_at    TEXT,
    deleted_at     TEXT,
    blocked_reason TEXT,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    repo_id      TEXT PRIMARY KEY,
    email        TEXT NOT NULL REFERENCES users(email),
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,
    target       TEXT,
    files        INTEGER,
    bytes        INTEGER,
    commit_id    TEXT,
    shared_with  TEXT,
    renamed      TEXT,
    message      TEXT,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    dry_run     INTEGER NOT NULL,
    ok          INTEGER,
    summary     TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Spalten, die nach der ersten Version hinzugekommen sind
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(repos)")}
        if "renamed" not in columns:
            self.db.execute("ALTER TABLE repos ADD COLUMN renamed TEXT")
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- Läufe ---
    def start_run(self, dry_run: bool) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (started_at, dry_run) VALUES (?, ?)", (now(), int(dry_run))
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, ok: bool, summary: str) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, ok = ?, summary = ? WHERE id = ?",
            (now(), int(ok), summary, run_id),
        )
        self.db.commit()

    # --- User ---
    def users(self) -> dict[str, sqlite3.Row]:
        return {r["email"]: r for r in self.db.execute("SELECT * FROM users")}

    def upsert_user(self, email: str, **fields) -> None:
        fields["updated_at"] = now()
        existing = self.db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE users SET {sets} WHERE email = ?", (*fields.values(), email))
        else:
            fields.setdefault("first_seen_at", now())
            cols = ", ".join(["email", *fields])
            marks = ", ".join(["?"] * (len(fields) + 1))
            self.db.execute(f"INSERT INTO users ({cols}) VALUES ({marks})", (email, *fields.values()))
        self.db.commit()

    # --- Bibliotheken ---
    def repos_of(self, email: str) -> dict[str, sqlite3.Row]:
        return {
            r["repo_id"]: r
            for r in self.db.execute("SELECT * FROM repos WHERE email = ?", (email,))
        }

    def clear_repos(self, email: str) -> None:
        self.db.execute("DELETE FROM repos WHERE email = ?", (email,))
        self.db.commit()

    def delete_repo(self, repo_id: str) -> None:
        self.db.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
        self.db.commit()

    def upsert_repo(self, repo_id: str, email: str, **fields) -> None:
        for key in ("shared_with", "renamed"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = now()
        cols = ["repo_id", "email", *fields]
        updates = ", ".join(f"{k} = excluded.{k}" for k in ["email", *fields])
        self.db.execute(
            f"INSERT INTO repos ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))}) "
            f"ON CONFLICT(repo_id) DO UPDATE SET {updates}",
            (repo_id, email, *fields.values()),
        )
        self.db.commit()
