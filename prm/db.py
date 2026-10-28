"""SQLite storage with forward-only migrations (PRAGMA user_version)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE profiles (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        enabled INTEGER NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL DEFAULT 0,
        current_version_id INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE managed_files (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('master','asset','original','reference','session')),
        rel_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        original_name TEXT,
        profile_id INTEGER REFERENCES profiles(id),
        job_id INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX ix_managed_sha ON managed_files(sha256);
    CREATE TABLE profile_versions (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL REFERENCES profiles(id),
        version_no INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        note TEXT,
        mode TEXT NOT NULL CHECK (mode IN ('master','operations')),
        match_rules TEXT NOT NULL,
        output TEXT NOT NULL,
        timestamp_policy TEXT NOT NULL,
        options TEXT NOT NULL,
        master_file_id INTEGER REFERENCES managed_files(id),
        base_file_id INTEGER REFERENCES managed_files(id),
        reference_fingerprint TEXT,
        UNIQUE (profile_id, version_no)
    );
    CREATE TABLE operations (
        id INTEGER PRIMARY KEY,
        version_id INTEGER NOT NULL REFERENCES profile_versions(id),
        seq INTEGER NOT NULL,
        op_type TEXT NOT NULL,
        params TEXT NOT NULL,
        UNIQUE (version_id, seq)
    );
    CREATE TABLE fingerprints (
        id INTEGER PRIMARY KEY,
        profile_id INTEGER NOT NULL REFERENCES profiles(id),
        kind TEXT NOT NULL CHECK (kind IN ('sha256','content')),
        value TEXT NOT NULL,
        note TEXT,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL,
        step TEXT,
        source_path TEXT NOT NULL,
        file_name TEXT NOT NULL,
        size INTEGER,
        download_created_ns INTEGER,
        download_modified_ns INTEGER,
        detected_ns INTEGER NOT NULL,
        sha256 TEXT,
        source_url TEXT,
        referrer_url TEXT,
        zone_raw TEXT,
        profile_id INTEGER,
        profile_version_id INTEGER,
        mode TEXT,
        match_reason TEXT,
        similarity REAL,
        backup_file_id INTEGER,
        staging_path TEXT,
        output_path TEXT,
        output_sha256 TEXT,
        timestamps TEXT,
        validation TEXT,
        op_results TEXT,
        error_code TEXT,
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        dry_run INTEGER NOT NULL DEFAULT 0,
        force INTEGER NOT NULL DEFAULT 0,
        started_ns INTEGER,
        finished_ns INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX ix_jobs_state ON jobs(state);
    CREATE TABLE job_events (
        id INTEGER PRIMARY KEY,
        job_id INTEGER REFERENCES jobs(id),
        ts INTEGER NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL,
        data TEXT
    );
    CREATE INDEX ix_events_job ON job_events(job_id);
    CREATE TABLE published_outputs (
        id INTEGER PRIMARY KEY,
        job_id INTEGER REFERENCES jobs(id),
        path TEXT NOT NULL COLLATE NOCASE,
        sha256 TEXT NOT NULL,
        published_ns INTEGER NOT NULL
    );
    CREATE INDEX ix_published_path ON published_outputs(path);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def now_ns() -> int:
    return time.time_ns()


class Database:
    """Thread-safe wrapper: one connection per thread, WAL journal, explicit transactions."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        self.migrate()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
            with self._all_lock:
                self._all.append(c)
        return c

    def close(self):
        """Close every connection (all threads) — required before replacing the database file."""
        with self._all_lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:
                    pass
            self._all.clear()
        self._local = threading.local()

    @contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def migrate(self):
        c = self.conn()
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema v{ver} is newer than this application (v{SCHEMA_VERSION}).")
        for i in range(ver, SCHEMA_VERSION):
            c.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(MIGRATIONS[i]):
                    c.execute(stmt)
                c.execute(f"PRAGMA user_version={i + 1}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def q(self, sql, args=()):
        return self.conn().execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql, args=()):
        return self.conn().execute(sql, args)

    # settings
    def get_setting(self, key, default=None):
        r = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r[0]) if r else default

    def set_setting(self, key, value):
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    def backup_to(self, dest: Path | str):
        """Consistent online copy using SQLite's backup API."""
        dst = sqlite3.connect(str(dest))
        try:
            self.conn().backup(dst)
        finally:
            dst.close()


def _split_sql(script: str):
    for stmt in script.split(";"):
        if stmt.strip():
            yield stmt
