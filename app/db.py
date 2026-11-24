"""SQLite 连接、schema、升级迁移与写事务。

冲突不再逐对物化：同页分组（scan_components）与训练登记
（training_scans.component_id）只维护「件号 -> 分组」与「训练件 -> 分组」，
凭清单号查询时再按分组直接展开冲突对。

所有写路径都通过 write_tx 开启的单事务执行：资料变动与供数结论（由同一份
分组/登记数据派生）一并提交；任一步失败整体回滚，沿用原状态。
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

-- 曾用于训练的扫描件号登记；component_id 指向其当前同页分组，
-- 使按清单号查询时只取组内训练登记，无需展开组内其他补扫件。
CREATE TABLE IF NOT EXISTS training_scans (
    scan_id      TEXT PRIMARY KEY,
    component_id INTEGER REFERENCES components(component_id),
    created_at   TEXT NOT NULL
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
"""

# 依赖迁移后列（training_scans.component_id）的索引，必须等迁移完成再建。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_scan_components_comp ON scan_components(component_id);
CREATE INDEX IF NOT EXISTS idx_training_scans_comp ON training_scans(component_id);
CREATE INDEX IF NOT EXISTS idx_manifest_items_scan ON manifest_items(scan_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None：自动提交，事务边界完全由 write_tx 显式控制
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_component(conn: sqlite3.Connection, scan_id: str) -> int:
    """确保件号有分量归属，返回其分量号。"""
    row = conn.execute(
        "SELECT component_id FROM scan_components WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    if row is not None:
        return row["component_id"]
    comp = conn.execute("INSERT INTO components DEFAULT VALUES").lastrowid
    conn.execute(
        "INSERT INTO scan_components (scan_id, component_id) VALUES (?, ?)",
        (scan_id, comp),
    )
    return comp


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧库升级为「只维护分组，不物化逐对冲突」的结构。

    - 删除旧版 conflicts 表（逐对冲突记录不再入库）。
    - 为 training_scans 增加 component_id，并从 scan_components 回填。
    - 旧库中缺失分组登记的件号（训练件、清单件、判定端点）补建单件分组，
      并按 active 同页判定重新合并，保证升级后原清单与判定仍可查询、
      冲突仍可派生。
    """
    conn.execute("DROP TABLE IF EXISTS conflicts")

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(training_scans)")}
    if "component_id" not in columns:
        conn.execute(
            "ALTER TABLE training_scans"
            " ADD COLUMN component_id INTEGER REFERENCES components(component_id)"
        )

    scan_ids: list[str] = []
    seen: set[str] = set()
    for table, column in (
        ("training_scans", "scan_id"),
        ("manifest_items", "scan_id"),
        ("same_page_edges", "scan_a"),
        ("same_page_edges", "scan_b"),
    ):
        for row in conn.execute(f"SELECT DISTINCT {column} AS scan_id FROM {table}"):
            if row["scan_id"] not in seen:
                seen.add(row["scan_id"])
                scan_ids.append(row["scan_id"])
    for scan_id in scan_ids:
        ensure_component(conn, scan_id)

    # 依据 active 判定重建连通性（旧库一致时为无操作；残留不一致时自愈）
    for edge in conn.execute(
        "SELECT scan_a, scan_b FROM same_page_edges WHERE active = 1"
    ).fetchall():
        comp_a = conn.execute(
            "SELECT component_id FROM scan_components WHERE scan_id = ?",
            (edge["scan_a"],),
        ).fetchone()["component_id"]
        comp_b = conn.execute(
            "SELECT component_id FROM scan_components WHERE scan_id = ?",
            (edge["scan_b"],),
        ).fetchone()["component_id"]
        if comp_a != comp_b:
            keep, drop = min(comp_a, comp_b), max(comp_a, comp_b)
            conn.execute(
                "UPDATE scan_components SET component_id = ? WHERE component_id = ?",
                (keep, drop),
            )
            conn.execute("DELETE FROM components WHERE component_id = ?", (drop,))

    conn.execute(
        "UPDATE training_scans SET component_id = ("
        "  SELECT sc.component_id FROM scan_components sc"
        "  WHERE sc.scan_id = training_scans.scan_id"
        ") WHERE component_id IS NULL"
        " OR component_id != ("
        "  SELECT sc.component_id FROM scan_components sc"
        "  WHERE sc.scan_id = training_scans.scan_id"
        ")"
    )


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        conn.executescript(INDEXES)
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
