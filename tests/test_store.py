"""增量存取与旧库升级的回归测试。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, store
from app.main import create_app


OLD_SCHEMA = """
CREATE TABLE components (
    component_id INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE TABLE scan_components (
    scan_id      TEXT PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES components(component_id)
);
CREATE INDEX idx_scan_components_comp ON scan_components(component_id);
CREATE TABLE training_scans (
    scan_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE same_page_edges (
    evidence_id  TEXT PRIMARY KEY,
    scan_a       TEXT NOT NULL,
    scan_b       TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE TABLE manifests (
    manifest_id TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);
CREATE TABLE manifest_items (
    manifest_id TEXT NOT NULL REFERENCES manifests(manifest_id),
    scan_id     TEXT NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY (manifest_id, scan_id)
);
CREATE TABLE conflicts (
    manifest_id      TEXT NOT NULL REFERENCES manifests(manifest_id),
    eval_scan_id     TEXT NOT NULL,
    training_scan_id TEXT NOT NULL,
    PRIMARY KEY (manifest_id, eval_scan_id, training_scan_id)
);
"""


def _build_old_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO components (component_id) VALUES (1)")
    conn.execute("INSERT INTO components (component_id) VALUES (2)")
    conn.executemany(
        "INSERT INTO scan_components (scan_id, component_id) VALUES (?, ?)",
        [("A", 1), ("B", 1), ("T", 1), ("S", 2)],
    )
    conn.execute(
        "INSERT INTO training_scans (scan_id, created_at) VALUES ('T', '2024-01-01')"
    )
    conn.executemany(
        "INSERT INTO same_page_edges"
        " (evidence_id, scan_a, scan_b, active, created_at)"
        " VALUES (?, ?, ?, 1, '2024-01-01')",
        [("E1", "A", "B"), ("E2", "B", "T")],
    )
    conn.execute("INSERT INTO manifests VALUES ('MFT-OLD', '2024-01-01')")
    conn.executemany(
        "INSERT INTO manifest_items (manifest_id, scan_id, position) VALUES (?, ?, ?)",
        [("MFT-OLD", "A", 0), ("MFT-OLD", "S", 1)],
    )
    conn.execute("INSERT INTO conflicts VALUES ('MFT-OLD', 'A', 'T')")
    conn.commit()
    conn.close()


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def test_old_database_upgrades_and_keeps_manifest_and_links(tmp_path):
    path = str(tmp_path / "old.db")
    _build_old_db(path)

    app = create_app(path)
    with TestClient(app) as client:
        # 原有判定仍可查
        links = client.get("/same-page-links").json()["items"]
        assert [l["evidence_id"] for l in links] == ["E1", "E2"]
        assert all(l["active"] == 1 for l in links)

        # 原清单升级后仍按原编号暂停供数，冲突对从分组派生
        r = client.get("/manifests/MFT-OLD")
        assert r.json() == {
            "manifest_id": "MFT-OLD",
            "status": "suspended",
            "conflicts": [{"eval_scan_id": "A", "training_scan_id": "T"}],
        }

        # 拆开训练件一侧的分组后，A 侧冲突消失，按原编号恢复供数
        assert client.delete("/same-page-links/E2").status_code == 200
        assert client.get("/manifests/MFT-OLD").json() == {
            "manifest_id": "MFT-OLD",
            "status": "published",
            "items": ["A", "S"],
        }

    # 逐对冲突表已移除，训练登记已回填分组
    assert "conflicts" not in _tables(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT component_id FROM training_scans WHERE scan_id = 'T'"
        ).fetchone()[0] is not None
    finally:
        conn.close()


def test_old_database_backfills_missing_component_rows(tmp_path):
    path = str(tmp_path / "partial.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    # 旧库中训练件/清单件缺少 scan_components 行
    conn.execute("INSERT INTO manifests VALUES ('M1', '2024-01-01')")
    conn.execute("INSERT INTO manifest_items VALUES ('M1', 'A', 0)")
    conn.execute("INSERT INTO training_scans VALUES ('T', '2024-01-01')")
    conn.commit()
    conn.close()

    with TestClient(create_app(path)) as client:
        assert client.get("/manifests/M1").json()["items"] == ["A"]
        # 补建分组后登记训练件，A 与 T 互不相干，仍供数
        client.post("/training-scans", json={"scan_id": "T"})
        assert client.get("/manifests/M1").json()["status"] == "published"


@pytest.fixture()
def conn(tmp_path):
    path = str(tmp_path / "store.db")
    db.init_db(path)
    c = db.connect(path)
    try:
        yield c
    finally:
        c.close()


def test_training_registration_only_touches_its_own_row(conn):
    with db.write_tx(conn):
        conn.execute("INSERT INTO components (component_id) VALUES (1)")
        conn.execute("INSERT INTO scan_components VALUES ('A', 1)")
        conn.execute("INSERT INTO scan_components VALUES ('B', 1)")
        conn.execute("INSERT INTO scan_components VALUES ('C', 1)")
        store.register_training_scan(conn, "A")

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        with db.write_tx(conn):
            # B/C 与 A 同组时新登记训练件，不应重写任何既有记录
            assert store.register_training_scan(conn, "B") is True
    finally:
        conn.set_trace_callback(None)

    writes = [s for s in statements if s.lstrip().upper().startswith(("UPDATE", "DELETE"))]
    assert writes == []
    assert not any("conflicts" in s.lower() for s in statements)


def test_deregistration_only_deletes_the_training_row(conn):
    with db.write_tx(conn):
        conn.execute("INSERT INTO components (component_id) VALUES (1)")
        conn.execute("INSERT INTO scan_components VALUES ('A', 1)")
        conn.execute("INSERT INTO scan_components VALUES ('B', 1)")
        store.register_training_scan(conn, "A")
        store.register_training_scan(conn, "B")

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        with db.write_tx(conn):
            store.deregister_training_scan(conn, "A")
    finally:
        conn.set_trace_callback(None)

    writes = [s for s in statements if s.lstrip().upper().startswith(("UPDATE", "DELETE"))]
    assert len(writes) == 1
    assert writes[0].lstrip().upper().startswith("DELETE FROM TRAINING_SCANS")


def test_manifest_query_skips_other_rescans_in_group(tmp_path):
    with TestClient(create_app(str(tmp_path / "q.db"))) as client:
        # 同页组：评测件 A、补扫件 B/C、训练件 T1/T2
        for eid, a, b in [
            ("E1", "A", "B"),
            ("E2", "B", "C"),
            ("E3", "C", "T1"),
            ("E4", "C", "T2"),
        ]:
            client.post(
                "/same-page-links",
                json={"evidence_id": eid, "scan_a": a, "scan_b": b},
            )
        client.post("/manifests", json={"items": ["A", "Z"]})  # 先无训练件，发布
        client.post("/training-scans", json={"scan_id": "T1"})
        client.post("/training-scans", json={"scan_id": "T2"})

        manifests = [m for m in _manifest_ids(tmp_path / "q.db")]
        assert len(manifests) == 1
        mid = manifests[0]

        r = client.get(f"/manifests/{mid}").json()
        # 只列出本清单件 A 与组内训练件的组合，不展开 B/C
        assert r["conflicts"] == [
            {"eval_scan_id": "A", "training_scan_id": "T1"},
            {"eval_scan_id": "A", "training_scan_id": "T2"},
        ]


def _manifest_ids(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT manifest_id FROM manifests")]
    finally:
        conn.close()


def test_split_keeps_still_valid_conflicts(tmp_path):
    with TestClient(create_app(str(tmp_path / "split.db"))) as client:
        # 组 {A, B, T}：A-T 与 A-B 两条判定
        for eid, a, b in [("E1", "A", "T"), ("E2", "A", "B")]:
            client.post(
                "/same-page-links",
                json={"evidence_id": eid, "scan_a": a, "scan_b": b},
            )
        mid = client.post("/manifests", json={"items": ["A", "B"]}).json()["manifest_id"]
        client.post("/training-scans", json={"scan_id": "T"})
        assert client.get(f"/manifests/{mid}").json()["conflicts"] == [
            {"eval_scan_id": "A", "training_scan_id": "T"},
            {"eval_scan_id": "B", "training_scan_id": "T"},
        ]

        # 拆开 A-B：组分裂为 {A, T} 与 {B}；B 的冲突消失，A 的仍然成立
        assert client.delete("/same-page-links/E2").status_code == 200
        assert client.get(f"/manifests/{mid}").json()["conflicts"] == [
            {"eval_scan_id": "A", "training_scan_id": "T"}
        ]


def test_withdrawal_moves_only_smaller_split_side(conn):
    # 链 A-B-C-D：撤回 A-B 后 {A} 与 {B,C,D} 分裂，只应迁移较小一侧 {A}
    with db.write_tx(conn):
        conn.execute("INSERT INTO components (component_id) VALUES (1)")
        for s in ("A", "B", "C", "D"):
            conn.execute("INSERT INTO scan_components VALUES (?, 1)", (s,))
        for eid, a, b in [("E1", "A", "B"), ("E2", "B", "C"), ("E3", "C", "D")]:
            store.register_same_page(conn, eid, a, b)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        with db.write_tx(conn):
            assert store.withdraw_same_page(conn, "E1") is True
    finally:
        conn.set_trace_callback(None)

    moved = [
        s for s in statements
        if "UPDATE scan_components SET component_id" in s
    ]
    # executemany 下按每条 execute 记录；A 侧迁移对新分量号一次 executemany，
    # 但驱动会对每行各回调一次，这里只校验迁移的件号集合。
    assert {r[0] for r in conn.execute(
        "SELECT scan_id FROM scan_components WHERE component_id = 1"
    )} == {"B", "C", "D"}
    assert moved  # 发生了迁移


def test_merge_moves_training_registration_with_component(conn):
    # T 先单独登记训练，再与 A/B 所在分组连成一片，训练登记须随组迁移
    with db.write_tx(conn):
        store.register_training_scan(conn, "T")
    with db.write_tx(conn):
        store.register_same_page(conn, "E1", "A", "B")
    with db.write_tx(conn):
        store.register_same_page(conn, "E2", "B", "T")

    comps = {
        r["scan_id"]: r["component_id"]
        for r in conn.execute("SELECT scan_id, component_id FROM scan_components")
    }
    assert comps["A"] == comps["B"] == comps["T"]
    reg = conn.execute(
        "SELECT component_id FROM training_scans WHERE scan_id = 'T'"
    ).fetchone()[0]
    assert reg == comps["A"]


def test_failed_write_rolls_back_material_changes(conn):
    with db.write_tx(conn):
        conn.execute("INSERT INTO components (component_id) VALUES (1)")
        conn.execute("INSERT INTO scan_components VALUES ('A', 1)")

    with pytest.raises(RuntimeError):
        with db.write_tx(conn):
            store.register_training_scan(conn, "A")
            raise RuntimeError("simulated failure")

    # 资料变动未提交，原状态保持不变（A 仍未被登记）
    assert conn.execute("SELECT COUNT(*) FROM training_scans").fetchone()[0] == 0
    assert conn.execute("SELECT component_id FROM scan_components WHERE scan_id='A'").fetchone()[0] == 1
