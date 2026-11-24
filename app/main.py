"""FastAPI 路由层。"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import db, store


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("不能为空")
    return value


class TrainingScanIn(BaseModel):
    scan_id: str = Field(min_length=1, max_length=256)

    @field_validator("scan_id")
    @classmethod
    def _check(cls, v: str) -> str:
        return _non_empty(v)


class SamePageLinkIn(BaseModel):
    evidence_id: str = Field(min_length=1, max_length=256)
    scan_a: str = Field(min_length=1, max_length=256)
    scan_b: str = Field(min_length=1, max_length=256)

    @field_validator("evidence_id", "scan_a", "scan_b")
    @classmethod
    def _check(cls, v: str) -> str:
        return _non_empty(v)

    @field_validator("scan_b")
    @classmethod
    def _distinct(cls, v: str, info) -> str:
        if "scan_a" in info.data and v == info.data["scan_a"]:
            raise ValueError("scan_a 与 scan_b 不能相同")
        return v


class ManifestIn(BaseModel):
    items: list[str] = Field(min_length=1, max_length=100000)

    @field_validator("items")
    @classmethod
    def _check(cls, v: list[str]) -> list[str]:
        return [_non_empty(item) for item in v]


def create_app(db_path: str) -> FastAPI:
    db.init_db(db_path)
    app = FastAPI(title="OCR 评测清单 API", version="1.0.0")

    def get_conn():
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    conn_dep = Depends(get_conn)

    @app.exception_handler(store.DomainError)
    async def domain_error_handler(_: Request, exc: store.DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc), **exc.extra},
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # -- 训练件登记 ---------------------------------------------------------

    @app.post("/training-scans", status_code=201)
    def post_training_scan(body: TrainingScanIn, response: Response, conn=conn_dep):
        """登记曾用于训练的扫描件号（重复登记幂等）。"""
        with db.write_tx(conn):
            created = store.register_training_scan(conn, body.scan_id)
        if not created:
            response.status_code = 200
        return {"scan_id": body.scan_id, "registered": True, "created": created}

    @app.delete("/training-scans/{scan_id}")
    def delete_training_scan(scan_id: str, conn=conn_dep):
        """注销训练件登记，相关冲突随之消除。"""
        with db.write_tx(conn):
            store.deregister_training_scan(conn, scan_id)
        return {"scan_id": scan_id, "registered": False}

    @app.get("/training-scans")
    def get_training_scans(conn=conn_dep):
        return {"items": store.list_training_scans(conn)}

    # -- 同页判定 -----------------------------------------------------------

    @app.post("/same-page-links", status_code=201)
    def post_same_page_link(body: SamePageLinkIn, response: Response, conn=conn_dep):
        """按证据号登记两件同页的判定；间接相连也算同页。"""
        with db.write_tx(conn):
            created = store.register_same_page(
                conn, body.evidence_id, body.scan_a, body.scan_b
            )
        if not created:
            response.status_code = 200
        return {"evidence_id": body.evidence_id, "active": True, "created": created}

    @app.delete("/same-page-links/{evidence_id}")
    def delete_same_page_link(evidence_id: str, conn=conn_dep):
        """按证据号撤回同页判定（重复撤回幂等）。"""
        with db.write_tx(conn):
            changed = store.withdraw_same_page(conn, evidence_id)
        return {"evidence_id": evidence_id, "active": False, "changed": changed}

    @app.get("/same-page-links")
    def get_same_page_links(conn=conn_dep):
        return {"items": store.list_same_page_links(conn)}

    # -- 评测清单 -----------------------------------------------------------

    @app.post("/manifests", status_code=201)
    def post_manifest(body: ManifestIn, conn=conn_dep):
        """提交评测件号清单：有冲突则退回（409），均无冲突才发布并发清单号。"""
        with db.write_tx(conn):
            manifest_id, items = store.submit_manifest(conn, body.items)
        return {"manifest_id": manifest_id, "status": "published", "items": items}

    @app.get("/manifests/{manifest_id}")
    def get_manifest(manifest_id: str, conn=conn_dep):
        """凭清单号查询：无冲突供出原清单；有冲突只返回冲突的评测件号与训练件号。"""
        result = store.get_manifest(conn, manifest_id)
        if result is None:
            raise store.NotFound(f"清单 {manifest_id} 不存在")
        return result

    return app


app = create_app(os.environ.get("DB_PATH", "ocr_eval.db"))
