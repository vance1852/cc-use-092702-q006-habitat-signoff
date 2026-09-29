"""选址综合结论会签服务的 SQLite 模式与事务辅助。"""

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
    role TEXT NOT NULL CHECK (role IN ('coordinator', 'forester', 'ecologist', 'engineer', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 各专业域冻结的调查协议版本（注册后不可改写）。
CREATE TABLE IF NOT EXISTS evidence_protocols (
    protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    domain TEXT NOT NULL CHECK (domain IN ('forestry', 'ecology', 'engineering')),
    title TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (protocol_id, version)
);

-- 证据批次：注册即封存；任何补充、撤回、失效都只追加新修订。
CREATE TABLE IF NOT EXISTS evidence_batches (
    batch_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL CHECK (domain IN ('forestry', 'ecology', 'engineering')),
    protocol_id TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    protocol_sha256 TEXT NOT NULL CHECK (length(protocol_sha256) = 64),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_batch_revisions (
    batch_id TEXT NOT NULL REFERENCES evidence_batches(batch_id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    change_note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, revision)
);

-- 评估对象（选址对象）的版本化结论草案。
CREATE TABLE IF NOT EXISTS conclusion_versions (
    subject_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    title TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft', 'blocked', 'published', 'superseded')),
    boundary_id TEXT NOT NULL,
    boundary_label TEXT NOT NULL,
    boundary_geometry_json TEXT NOT NULL,
    boundary_source TEXT NOT NULL,
    boundary_sha256 TEXT NOT NULL CHECK (length(boundary_sha256) = 64),
    freeze_json TEXT NOT NULL,
    freeze_sha256 TEXT NOT NULL CHECK (length(freeze_sha256) = 64),
    blocked_reason TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (subject_id, version_no)
);

-- 同一评估对象同时只能有一个可签署草案；被阻断的草案不再占用名额。
CREATE UNIQUE INDEX IF NOT EXISTS one_open_draft_per_subject
ON conclusion_versions(subject_id)
WHERE state = 'draft';

-- 结论版本对三个专业域证据批次修订的冻结引用。
CREATE TABLE IF NOT EXISTS conclusion_evidence_refs (
    ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    step TEXT NOT NULL CHECK (step IN ('forestry', 'ecology', 'engineering')),
    batch_id TEXT NOT NULL,
    batch_revision INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    protocol_id TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    protocol_sha256 TEXT NOT NULL CHECK (length(protocol_sha256) = 64),
    UNIQUE (subject_id, version_no, step),
    FOREIGN KEY (subject_id, version_no) REFERENCES conclusion_versions(subject_id, version_no),
    FOREIGN KEY (batch_id, batch_revision) REFERENCES evidence_batch_revisions(batch_id, revision)
);

-- 顺序会签记录；失效只改状态并留痕，行本身不删除。
CREATE TABLE IF NOT EXISTS signatures (
    signature_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    step TEXT NOT NULL CHECK (step IN ('forestry', 'ecology', 'engineering')),
    sign_order INTEGER NOT NULL CHECK (sign_order BETWEEN 1 AND 3),
    signer_id TEXT NOT NULL REFERENCES users(user_id),
    basis_summary TEXT NOT NULL,
    signature_sha256 TEXT NOT NULL CHECK (length(signature_sha256) = 64),
    prev_signature_sha256 TEXT CHECK (prev_signature_sha256 IS NULL OR length(prev_signature_sha256) = 64),
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'invalidated')),
    invalidate_reason TEXT,
    signed_at TEXT NOT NULL,
    invalidated_at TEXT
);

-- 每个职责步骤在一个版本上至多保留一个有效签署。
CREATE UNIQUE INDEX IF NOT EXISTS one_valid_signature_per_step
ON signatures(subject_id, version_no, step)
WHERE status = 'valid';

CREATE INDEX IF NOT EXISTS signatures_version_index
ON signatures(subject_id, version_no, sign_order);

-- 回避关系：同一版本对同一人只能申报一次，申报后不可撤回。
CREATE TABLE IF NOT EXISTS recusals (
    recusal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    reason TEXT NOT NULL,
    declared_by TEXT NOT NULL REFERENCES users(user_id),
    declared_at TEXT NOT NULL,
    UNIQUE (subject_id, version_no, user_id),
    FOREIGN KEY (subject_id, version_no) REFERENCES conclusion_versions(subject_id, version_no)
);

-- 原子发布产生的正式决定；重复发布不产生第二份决定。
CREATE TABLE IF NOT EXISTS publications (
    decision_no TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    freeze_sha256 TEXT NOT NULL CHECK (length(freeze_sha256) = 64),
    evidence_integrity_sha256 TEXT NOT NULL CHECK (length(evidence_integrity_sha256) = 64),
    record_json TEXT NOT NULL,
    published_by TEXT NOT NULL REFERENCES users(user_id),
    published_at TEXT NOT NULL,
    UNIQUE (subject_id, version_no)
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

CREATE INDEX IF NOT EXISTS audit_events_entity_index
ON audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "evidence_protocols", "evidence_batches", "evidence_batch_revisions",
    "conclusion_versions", "conclusion_evidence_refs", "signatures", "recusals",
    "publications", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    HTTP 服务使用线程服务器，连接可能在请求线程间复用；所有写操作均以
    BEGIN IMMEDIATE 开始并由 busy_timeout 兜底，因此允许跨线程共享文件库连接。
    """

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
    """初始化数据库结构，重复执行不改变已有数据。"""

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
