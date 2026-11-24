"""核心领域逻辑：训练件登记、同页判定、清单发布与冲突派生。

不变量：
1. scan_components 始终等于 active 同页边的连通分量（传递闭包），
   training_scans.component_id 始终等于该训练件当前所在分量。
2. 不逐对物化冲突：凭清单号查询时按清单件所在分量直接关联训练登记
   （JOIN training_scans.component_id），组内其他补扫件不参与取数。
3. 清单供数状态由上述分组/登记在读取时派生：无冲突 -> 按原编号供出原清单；
   有冲突 -> 暂停供数，凭清单号只返回冲突的评测件号与训练件号。

每个公开写操作都必须包在 db.write_tx 单事务内调用：登记/判定变动与供数结论
一并生效，任一步失败整体回滚，沿用原状态。写路径只更新受影响的件号（及其
同页分组成员），不再重写任何逐对冲突记录。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from .db import ensure_component as _ensure_component


class DomainError(Exception):
    """领域错误，status_code 映射为 HTTP 状态码。"""

    status_code = 400

    def __init__(self, message: str = "", **extra):
        super().__init__(message or self.__class__.__name__)
        self.extra = extra


class NotFound(DomainError):
    status_code = 404


class EvidenceMismatch(DomainError):
    status_code = 409


class ManifestConflict(DomainError):
    status_code = 409

    def __init__(self, conflicts: list[dict]):
        super().__init__("清单中存在曾用于训练或与训练件同页的件号，已退回", conflicts=conflicts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 连通分量（增量维护）
# ---------------------------------------------------------------------------

def _component_of(conn: sqlite3.Connection, scan_id: str) -> int | None:
    row = conn.execute(
        "SELECT component_id FROM scan_components WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    return None if row is None else row["component_id"]


def _component_members(conn: sqlite3.Connection, component_id: int) -> list[str]:
    return [
        r["scan_id"]
        for r in conn.execute(
            "SELECT scan_id FROM scan_components WHERE component_id = ?",
            (component_id,),
        )
    ]


def _move_component(
    conn: sqlite3.Connection, scan_ids: list[str], component_id: int
) -> None:
    """把件号及其训练登记一并迁到指定分量（只动受影响的件号）。"""
    if not scan_ids:
        return
    conn.executemany(
        "UPDATE scan_components SET component_id = ? WHERE scan_id = ?",
        [(component_id, s) for s in scan_ids],
    )
    conn.executemany(
        "UPDATE training_scans SET component_id = ? WHERE scan_id = ?",
        [(component_id, s) for s in scan_ids],
    )


def _merge_components(conn: sqlite3.Connection, c1: int, c2: int) -> int:
    """合并两个分量，返回保留的分量号。

    只迁移被并入一侧的成员；保留成员较多的一侧以减少写入量。
    """
    members_1 = _component_members(conn, c1)
    members_2 = _component_members(conn, c2)
    keep, drop, moving = (
        (c1, c2, members_2) if len(members_1) >= len(members_2) else (c2, c1, members_1)
    )
    _move_component(conn, moving, keep)
    conn.execute("DELETE FROM components WHERE component_id = ?", (drop,))
    return keep


def _split_after_withdrawal(
    conn: sqlite3.Connection, a: str, b: str, members: list[str]
) -> bool:
    """撤回边 (a, b) 后检查原分量是否分裂。

    仅在原分量成员范围内做 BFS；若 a 到 b 不再连通，把较小的一侧迁往新分量
    （较大一侧保留原分量号，写入量最小）。返回是否发生了分裂。
    """
    placeholders = ",".join("?" * len(members))
    edges = conn.execute(
        f"SELECT scan_a, scan_b FROM same_page_edges "
        f"WHERE active = 1 AND scan_a IN ({placeholders}) AND scan_b IN ({placeholders})",
        (*members, *members),
    ).fetchall()
    adjacency: dict[str, list[str]] = {}
    for e in edges:
        adjacency.setdefault(e["scan_a"], []).append(e["scan_b"])
        adjacency.setdefault(e["scan_b"], []).append(e["scan_a"])

    seen = {a}
    stack = [a]
    while stack:
        x = stack.pop()
        for y in adjacency.get(x, ()):
            if y not in seen:
                seen.add(y)
                stack.append(y)
    if b in seen:
        return False  # 仍有其他路径相连，分量不变

    other = [s for s in members if s not in seen]
    moving = list(seen) if len(seen) <= len(other) else other
    new_comp = conn.execute("INSERT INTO components DEFAULT VALUES").lastrowid
    _move_component(conn, moving, new_comp)
    return True


# ---------------------------------------------------------------------------
# 训练件登记
# ---------------------------------------------------------------------------

def _trainings_in_component(conn: sqlite3.Connection, component_id: int) -> list[str]:
    return [
        r["scan_id"]
        for r in conn.execute(
            "SELECT scan_id FROM training_scans"
            " WHERE component_id = ? ORDER BY scan_id",
            (component_id,),
        )
    ]


def register_training_scan(conn: sqlite3.Connection, scan_id: str) -> bool:
    """登记曾用于训练的扫描件号。返回是否新登记（重复登记幂等）。

    仅写入该件号本身及其分组归属，不触发任何冲突重算。
    """
    comp = _ensure_component(conn, scan_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO training_scans (scan_id, component_id, created_at)"
        " VALUES (?, ?, ?)",
        (scan_id, comp, _now()),
    )
    return cur.rowcount > 0


def deregister_training_scan(conn: sqlite3.Connection, scan_id: str) -> None:
    """注销训练件登记（其冲突在读取时自然消失）。"""
    cur = conn.execute("DELETE FROM training_scans WHERE scan_id = ?", (scan_id,))
    if cur.rowcount == 0:
        raise NotFound(f"训练件 {scan_id} 未登记")


def list_training_scans(conn: sqlite3.Connection) -> list[str]:
    return [
        r["scan_id"]
        for r in conn.execute("SELECT scan_id FROM training_scans ORDER BY scan_id")
    ]


# ---------------------------------------------------------------------------
# 同页判定（按证据号登记 / 撤回）
# ---------------------------------------------------------------------------

def register_same_page(
    conn: sqlite3.Connection, evidence_id: str, scan_a: str, scan_b: str
) -> bool:
    """按证据号登记两件同页的判定。间接相连（传递）也算同页。

    返回是否发生了状态变化（重复登记同一有效判定为幂等）。
    已撤回的同一证据号可重新登记；证据号不得改指其他件号对。
    """
    row = conn.execute(
        "SELECT scan_a, scan_b, active FROM same_page_edges WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if row is not None:
        if {row["scan_a"], row["scan_b"]} != {scan_a, scan_b}:
            raise EvidenceMismatch(f"证据号 {evidence_id} 已用于其他件号对")
        if row["active"]:
            return False
        conn.execute(
            "UPDATE same_page_edges SET active = 1, withdrawn_at = NULL"
            " WHERE evidence_id = ?",
            (evidence_id,),
        )
    else:
        conn.execute(
            "INSERT INTO same_page_edges (evidence_id, scan_a, scan_b, active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (evidence_id, scan_a, scan_b, _now()),
        )

    comp_a = _ensure_component(conn, scan_a)
    comp_b = _ensure_component(conn, scan_b)
    if comp_a != comp_b:
        _merge_components(conn, comp_a, comp_b)
    return True


def withdraw_same_page(conn: sqlite3.Connection, evidence_id: str) -> bool:
    """按证据号撤回同页判定。返回是否发生了状态变化（重复撤回幂等）。"""
    row = conn.execute(
        "SELECT scan_a, scan_b, active FROM same_page_edges WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"证据号 {evidence_id} 不存在")
    if not row["active"]:
        return False
    conn.execute(
        "UPDATE same_page_edges SET active = 0, withdrawn_at = ? WHERE evidence_id = ?",
        (_now(), evidence_id),
    )
    comp = _component_of(conn, row["scan_a"])
    if comp is not None:
        members = _component_members(conn, comp)
        _split_after_withdrawal(conn, row["scan_a"], row["scan_b"], members)
    return True


def list_same_page_links(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT evidence_id, scan_a, scan_b, active, created_at, withdrawn_at"
            " FROM same_page_edges ORDER BY evidence_id"
        )
    ]


# ---------------------------------------------------------------------------
# 评测清单
# ---------------------------------------------------------------------------

def _new_manifest_id(conn: sqlite3.Connection) -> str:
    while True:
        manifest_id = "MFT-" + uuid.uuid4().hex[:12].upper()
        exists = conn.execute(
            "SELECT 1 FROM manifests WHERE manifest_id = ?", (manifest_id,)
        ).fetchone()
        if exists is None:
            return manifest_id


def submit_manifest(conn: sqlite3.Connection, items: list[str]) -> tuple[str, list[str]]:
    """提交评测件号清单。

    任一件曾用于训练或与训练件（直接或间接）同页 -> 抛出 ManifestConflict，
    事务回滚，清单不发布。均无冲突才发布并返回清单号。
    """
    items = list(dict.fromkeys(items))  # 去重保序
    conflicts: list[dict] = []
    for scan_id in items:
        comp = _component_of(conn, scan_id)
        if comp is None:
            continue  # 从未出现过的件号：无训练登记、无同页判定，必无冲突
        for training_id in _trainings_in_component(conn, comp):
            conflicts.append(
                {"eval_scan_id": scan_id, "training_scan_id": training_id}
            )
    if conflicts:
        raise ManifestConflict(conflicts)

    manifest_id = _new_manifest_id(conn)
    conn.execute(
        "INSERT INTO manifests (manifest_id, created_at) VALUES (?, ?)",
        (manifest_id, _now()),
    )
    for position, scan_id in enumerate(items):
        conn.execute(
            "INSERT INTO manifest_items (manifest_id, scan_id, position)"
            " VALUES (?, ?, ?)",
            (manifest_id, scan_id, position),
        )
        # 让评测件进入分量表，后续训练登记/同页判定变动才能定位到它
        _ensure_component(conn, scan_id)
    return manifest_id, items


def get_manifest(conn: sqlite3.Connection, manifest_id: str) -> dict | None:
    """凭清单号查询。只读 SELECT，无需重新串联同页判定。

    只取本清单的评测件，按「件号 -> 分量 -> 训练登记」直接关联，
    不展开组内其他补扫件。无冲突：供出原清单；有冲突：暂停供数，
    只返回冲突的评测件号与训练件号。
    """
    manifest = conn.execute(
        "SELECT manifest_id FROM manifests WHERE manifest_id = ?", (manifest_id,)
    ).fetchone()
    if manifest is None:
        return None
    conflicts = conn.execute(
        "SELECT mi.scan_id AS eval_scan_id, t.scan_id AS training_scan_id"
        " FROM manifest_items mi"
        " JOIN scan_components sc ON sc.scan_id = mi.scan_id"
        " JOIN training_scans t ON t.component_id = sc.component_id"
        " WHERE mi.manifest_id = ?"
        " ORDER BY eval_scan_id, training_scan_id",
        (manifest_id,),
    ).fetchall()
    if conflicts:
        return {
            "manifest_id": manifest_id,
            "status": "suspended",
            "conflicts": [dict(r) for r in conflicts],
        }
    items = conn.execute(
        "SELECT scan_id FROM manifest_items WHERE manifest_id = ? ORDER BY position",
        (manifest_id,),
    ).fetchall()
    return {
        "manifest_id": manifest_id,
        "status": "published",
        "items": [r["scan_id"] for r in items],
    }
