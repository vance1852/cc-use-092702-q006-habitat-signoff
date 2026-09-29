"""选址会签服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('forester', 'ecologist', 'engineer', 'publisher', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS survey_protocols (
    protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    discipline TEXT NOT NULL,
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (protocol_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS spatial_boundaries (
    boundary_id TEXT PRIMARY KEY,
    discipline TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    crs TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessment_subjects (
    subject_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    discipline_scope TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_batches (
    batch_id TEXT PRIMARY KEY,
    discipline TEXT NOT NULL,
    protocol_id TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'withdrawn')),
    collected_by TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    note TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT,
    FOREIGN KEY (protocol_id, protocol_version) REFERENCES survey_protocols(protocol_id, version)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES evidence_batches(batch_id),
    source_ref TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'withdrawn', 'invalidated')),
    imported_by TEXT NOT NULL REFERENCES users(user_id),
    imported_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidate_reason TEXT,
    UNIQUE (batch_id, source_ref)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- 评估对象的版本化结论草案。signing=会签中；blocked=证据变动阻断；published=已发布归档（不可变）。
CREATE TABLE IF NOT EXISTS conclusion_versions (
    version_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES assessment_subjects(subject_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    discipline TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('signing', 'blocked', 'published')),
    protocol_id TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    protocol_sha256 TEXT NOT NULL CHECK (length(protocol_sha256) = 64),
    boundary_id TEXT NOT NULL,
    boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    summary_json TEXT NOT NULL,
    basis_hash TEXT NOT NULL CHECK (length(basis_hash) = 64),
    supersedes_version TEXT,
    blocked_reason TEXT,
    blocked_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT REFERENCES users(user_id),
    UNIQUE (subject_id, version_no)
);

-- 同一评估对象至多一份处于会签中的草案，阻断后允许另立新草案。
CREATE UNIQUE INDEX IF NOT EXISTS one_open_version_per_subject
ON conclusion_versions(subject_id)
WHERE state = 'signing';

CREATE TABLE IF NOT EXISTS conclusion_manifest_items (
    manifest_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES conclusion_versions(version_id),
    batch_id TEXT NOT NULL,
    item_id INTEGER,
    item_sha256 TEXT NOT NULL CHECK (length(item_sha256) = 64),
    frozen_state TEXT NOT NULL,
    UNIQUE (version_id, item_id)
);

CREATE TABLE IF NOT EXISTS signoff_steps (
    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES conclusion_versions(version_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    UNIQUE (version_id, sequence)
);

CREATE TABLE IF NOT EXISTS signoffs (
    signoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id INTEGER NOT NULL UNIQUE REFERENCES signoff_steps(step_id),
    version_id TEXT NOT NULL REFERENCES conclusion_versions(version_id),
    signer_id TEXT NOT NULL REFERENCES users(user_id),
    signer_role TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    signed_fingerprint TEXT NOT NULL CHECK (length(signed_fingerprint) = 64),
    note TEXT,
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'invalidated')),
    signed_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidate_reason TEXT
);

-- 重复签署不产生第二份决定：同一版本同一签署人只保留一行。
CREATE UNIQUE INDEX IF NOT EXISTS one_signoff_per_signer_per_version
ON signoffs(version_id, signer_id);

CREATE TABLE IF NOT EXISTS conflicts_of_interest (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL REFERENCES conclusion_versions(version_id),
    user_id TEXT NOT NULL REFERENCES users(user_id),
    reason TEXT NOT NULL,
    declared_by TEXT NOT NULL REFERENCES users(user_id),
    declared_at TEXT NOT NULL,
    UNIQUE (version_id, user_id)
);

CREATE TABLE IF NOT EXISTS conclusion_publications (
    publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id TEXT NOT NULL UNIQUE REFERENCES conclusion_versions(version_id),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    basis_hash TEXT NOT NULL CHECK (length(basis_hash) = 64),
    signoff_fingerprint TEXT NOT NULL CHECK (length(signoff_fingerprint) = 64),
    published_by TEXT NOT NULL REFERENCES users(user_id),
    published_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "survey_protocols", "spatial_boundaries", "assessment_subjects",
    "evidence_batches", "evidence_items", "idempotency_keys", "conclusion_versions",
    "conclusion_manifest_items", "signoff_steps", "signoffs", "conflicts_of_interest",
    "conclusion_publications", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
