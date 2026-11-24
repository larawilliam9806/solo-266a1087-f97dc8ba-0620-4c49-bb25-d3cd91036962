"""SQLite 连接与 schema。

所有写路径都通过 write_tx 开启的单事务执行：训练登记、同页判定的变动
与供数结论（conflicts 表）一并提交；任一步失败整体回滚，沿用原状态。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
-- 连通分量发号表（同页判定的传递闭包按分量增量物化）
CREATE TABLE IF NOT EXISTS components (
    component_id INTEGER PRIMARY KEY AUTOINCREMENT
);

-- 每个扫描件当前所属的连通分量
CREATE TABLE IF NOT EXISTS scan_components (
    scan_id      TEXT PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES components(component_id)
);
CREATE INDEX IF NOT EXISTS idx_scan_components_comp ON scan_components(component_id);

-- 曾用于训练的扫描件号登记
CREATE TABLE IF NOT EXISTS training_scans (
    scan_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- 同页判定：按证据号登记 / 撤回
CREATE TABLE IF NOT EXISTS same_page_edges (
    evidence_id  TEXT PRIMARY KEY,
    scan_a       TEXT NOT NULL,
    scan_b       TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    withdrawn_at TEXT
);

-- 已发布的评测清单
CREATE TABLE IF NOT EXISTS manifests (
    manifest_id TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest_items (
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id),
    scan_id     TEXT NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY (manifest_id, scan_id)
);
CREATE INDEX IF NOT EXISTS idx_manifest_items_scan ON manifest_items(scan_id);

-- 物化的冲突对：清单内评测件与训练件（直接或间接同页）
-- 有任一行存在即表示该清单暂停供数；清空即按原编号恢复供数。
CREATE TABLE IF NOT EXISTS conflicts (
    manifest_id      TEXT NOT NULL REFERENCES manifests(manifest_id),
    eval_scan_id     TEXT NOT NULL,
    training_scan_id TEXT NOT NULL,
    PRIMARY KEY (manifest_id, eval_scan_id, training_scan_id)
);
CREATE INDEX IF NOT EXISTS idx_conflicts_eval ON conflicts(eval_scan_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None：自动提交，事务边界完全由 write_tx 显式控制
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """单事务写：全部成功才提交，任一步失败整体回滚，沿用原状态。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
