"""端到端 API 测试：覆盖登记、判定、发布、暂停、恢复的完整生命周期。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def register_training(client, scan_id):
    r = client.post("/training-scans", json={"scan_id": scan_id})
    assert r.status_code in (200, 201), r.text
    return r


def link(client, evidence_id, a, b):
    r = client.post(
        "/same-page-links",
        json={"evidence_id": evidence_id, "scan_a": a, "scan_b": b},
    )
    assert r.status_code in (200, 201), r.text
    return r


def unlink(client, evidence_id):
    r = client.delete(f"/same-page-links/{evidence_id}")
    assert r.status_code == 200, r.text
    return r


def publish(client, items):
    r = client.post("/manifests", json={"items": items})
    assert r.status_code == 201, r.text
    return r.json()["manifest_id"]


# ---------------------------------------------------------------------------
# 清单提交：退回与发布
# ---------------------------------------------------------------------------

def test_publish_clean_manifest(client):
    r = client.post("/manifests", json={"items": ["S1", "S2", "S2", "S3"]})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "published"
    assert body["items"] == ["S1", "S2", "S3"]  # 去重保序
    mid = body["manifest_id"]

    r = client.get(f"/manifests/{mid}")
    assert r.status_code == 200
    assert r.json() == {"manifest_id": mid, "status": "published",
                        "items": ["S1", "S2", "S3"]}


def test_reject_manifest_containing_training_scan(client):
    register_training(client, "T1")
    r = client.post("/manifests", json={"items": ["S1", "T1"]})
    assert r.status_code == 409
    assert r.json()["conflicts"] == [{"eval_scan_id": "T1", "training_scan_id": "T1"}]
    assert "manifest_id" not in r.json()


def test_reject_manifest_indirect_same_page(client):
    # T1 -A- B：B 与训练件 T1 间接相连，也算同页
    register_training(client, "T1")
    link(client, "E1", "T1", "A")
    link(client, "E2", "A", "B")
    r = client.post("/manifests", json={"items": ["B", "S9"]})
    assert r.status_code == 409
    assert r.json()["conflicts"] == [{"eval_scan_id": "B", "training_scan_id": "T1"}]


def test_failed_submission_leaves_state_unchanged(client):
    register_training(client, "T1")
    assert client.post("/manifests", json={"items": ["T1"]}).status_code == 409
    client.delete("/training-scans/T1")
    # 冲突源消除后同一批件号可以发布，说明退回时未写入任何残留状态
    r = client.post("/manifests", json={"items": ["T1"]})
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# 已发布清单：出现冲突即暂停，消除后按原编号恢复
# ---------------------------------------------------------------------------

def test_suspend_on_new_same_page_link_then_recover(client):
    mid = publish(client, ["A", "S1"])
    register_training(client, "T1")

    # 尚无关联，仍供出原清单
    assert client.get(f"/manifests/{mid}").json()["status"] == "published"

    # 新判定 A 与 T1 同页 -> 暂停供数，只查到冲突对
    link(client, "E1", "A", "T1")
    r = client.get(f"/manifests/{mid}")
    assert r.json() == {
        "manifest_id": mid,
        "status": "suspended",
        "conflicts": [{"eval_scan_id": "A", "training_scan_id": "T1"}],
    }
    assert "items" not in r.json()

    # 撤回判定 -> 冲突消除，按原编号供出原清单
    unlink(client, "E1")
    r = client.get(f"/manifests/{mid}")
    assert r.json() == {"manifest_id": mid, "status": "published",
                        "items": ["A", "S1"]}


def test_suspend_on_new_training_registration(client):
    link(client, "E1", "A", "B")
    mid = publish(client, ["A"])
    assert client.get(f"/manifests/{mid}").json()["status"] == "published"

    # 新登记的训练件 B 与 A 同页 -> 暂停
    register_training(client, "B")
    r = client.get(f"/manifests/{mid}")
    assert r.json()["status"] == "suspended"
    assert r.json()["conflicts"] == [{"eval_scan_id": "A", "training_scan_id": "B"}]

    # 注销训练件 -> 恢复
    client.delete("/training-scans/B")
    assert client.get(f"/manifests/{mid}").json()["status"] == "published"


def test_transitive_conflict_and_partial_withdrawal(client):
    # X -Y- Z，训练件在 Z，清单含 X -> 间接同页冲突
    link(client, "E1", "X", "Y")
    link(client, "E2", "Y", "Z")
    mid = publish(client, ["X"])
    register_training(client, "Z")
    assert client.get(f"/manifests/{mid}").json()["conflicts"] == [
        {"eval_scan_id": "X", "training_scan_id": "Z"}
    ]

    # 撤回 E2 后 X/Y 与 Z 断开 -> 恢复
    unlink(client, "E2")
    assert client.get(f"/manifests/{mid}").json()["status"] == "published"


def test_withdrawal_without_split_keeps_suspension(client):
    # 环：A-B-C-A，撤回一条边分量不分裂，冲突仍在
    link(client, "E1", "A", "B")
    link(client, "E2", "B", "C")
    link(client, "E3", "A", "C")
    mid = publish(client, ["A"])
    register_training(client, "C")
    assert client.get(f"/manifests/{mid}").json()["status"] == "suspended"

    unlink(client, "E3")  # 仍有路径 A-B-C
    assert client.get(f"/manifests/{mid}").json()["status"] == "suspended"

    unlink(client, "E2")  # 彻底断开
    assert client.get(f"/manifests/{mid}").json()["status"] == "published"


def test_direct_training_conflict_resolved_by_deregister(client):
    mid = publish(client, ["A"])
    register_training(client, "A")
    r = client.get(f"/manifests/{mid}")
    assert r.json()["conflicts"] == [{"eval_scan_id": "A", "training_scan_id": "A"}]

    client.delete("/training-scans/A")
    r = client.get(f"/manifests/{mid}")
    assert r.json() == {"manifest_id": mid, "status": "published", "items": ["A"]}


def test_multiple_manifests_and_multiple_conflicts(client):
    m1 = publish(client, ["A"])
    m2 = publish(client, ["B", "S1"])
    link(client, "E1", "A", "B")
    register_training(client, "B")

    # 分量 {A, B} 含训练件 B：m1 的 A、m2 的 B 都冲突
    assert client.get(f"/manifests/{m1}").json()["conflicts"] == [
        {"eval_scan_id": "A", "training_scan_id": "B"}
    ]
    assert client.get(f"/manifests/{m2}").json()["conflicts"] == [
        {"eval_scan_id": "B", "training_scan_id": "B"}
    ]

    unlink(client, "E1")
    assert client.get(f"/manifests/{m1}").json()["status"] == "published"
    # m2 的 B 本身是训练件，冲突仍在
    assert client.get(f"/manifests/{m2}").json()["status"] == "suspended"


# ---------------------------------------------------------------------------
# 登记与判定的边界行为
# ---------------------------------------------------------------------------

def test_training_registration_idempotent(client):
    register_training(client, "T1")
    r = register_training(client, "T1")
    assert r.status_code == 200
    assert r.json()["created"] is False
    assert client.get("/training-scans").json()["items"] == ["T1"]


def test_deregister_unknown_training_scan_404(client):
    assert client.delete("/training-scans/NOPE").status_code == 404


def test_evidence_id_reuse_with_different_pair_rejected(client):
    link(client, "E1", "A", "B")
    r = client.post(
        "/same-page-links",
        json={"evidence_id": "E1", "scan_a": "A", "scan_b": "C"},
    )
    assert r.status_code == 409


def test_same_page_link_idempotent_and_reactivation(client):
    link(client, "E1", "A", "B")
    r = link(client, "E1", "A", "B")
    assert r.status_code == 200 and r.json()["created"] is False

    unlink(client, "E1")
    unlink(client, "E1")  # 重复撤回幂等
    r = link(client, "E1", "B", "A")  # 同一对（顺序无关）重新登记
    assert r.status_code == 201 and r.json()["created"] is True

    links = client.get("/same-page-links").json()["items"]
    assert len(links) == 1 and links[0]["active"] == 1


def test_withdraw_unknown_evidence_404(client):
    assert client.delete("/same-page-links/NOPE").status_code == 404


def test_same_scan_pair_rejected(client):
    r = client.post(
        "/same-page-links",
        json={"evidence_id": "E1", "scan_a": "A", "scan_b": "A"},
    )
    assert r.status_code == 422


def test_unknown_manifest_404(client):
    assert client.get("/manifests/MFT-UNKNOWN").status_code == 404


def test_empty_or_blank_input_rejected(client):
    assert client.post("/manifests", json={"items": []}).status_code == 422
    assert client.post("/manifests", json={"items": ["  "]}).status_code == 422
    assert client.post("/training-scans", json={"scan_id": " "}).status_code == 422
