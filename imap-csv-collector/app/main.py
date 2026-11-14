"""FastAPI 应用：采集触发、样品结果查询与冲突核对。"""
from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db, review
from .collector import collect


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="仪器邮件 CSV 采集服务",
    description=(
        "从 IMAP 邮箱采集仪器 CSV 附件并入库，支持按样品编号查询检测结果；"
        "对同一样品+检测项目+单位下数值不一致的结果组提供冲突核对"
        "（可信选择、过期提交拒绝、新结果触发复核）。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


class CollectRequest(BaseModel):
    host: str = Field(..., description="IMAP 服务器地址")
    port: int = Field(..., ge=1, le=65535, description="IMAP 端口")
    folder: str = Field(..., description="邮箱文件夹，如 INBOX")
    username: str
    password: str
    use_ssl: bool = Field(False, description="是否使用 IMAP over SSL")


class CollectResponse(BaseModel):
    status: str
    mailbox_key: str
    folder: str
    uidvalidity: int
    resume_from_uid: int
    scanned_uids: list[int]
    ingested: list[dict[str, Any]]
    skipped_duplicates: list[dict[str, Any]]
    stopped: dict[str, Any] | None
    total_new_records: int


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/collect", response_model=CollectResponse)
def trigger_collect(req: CollectRequest) -> dict[str, Any]:
    report = collect(
        host=req.host,
        port=req.port,
        folder=req.folder,
        username=req.username,
        password=req.password,
        use_ssl=req.use_ssl,
    )
    return report.to_dict()


@app.get("/results/{sample_no}")
def get_results(sample_no: str) -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.query_results_by_sample(conn, sample_no)
    if not rows:
        raise HTTPException(status_code=404, detail=f"未找到样品 {sample_no} 的结果")
    return {
        "sample_no": sample_no,
        "count": len(rows),
        "results": [dict(row) for row in rows],
    }


@app.get("/samples")
def get_samples() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_samples(conn)
    return {"count": len(rows), "samples": [dict(row) for row in rows]}


@app.get("/ingest/state")
def get_ingest_state() -> dict[str, Any]:
    with db.connect() as conn:
        states = [
            dict(row)
            for row in conn.execute("SELECT * FROM ingest_state ORDER BY mailbox_key")
        ]
    return {"count": len(states), "states": states}


@app.get("/ingest/deliveries")
def get_deliveries() -> dict[str, Any]:
    with db.connect() as conn:
        rows = db.list_deliveries(conn)
    return {"count": len(rows), "deliveries": [dict(row) for row in rows]}


@app.post("/admin/reset")
def reset_database() -> dict[str, str]:
    """清空全部业务数据（演示/测试用，生产环境请经网关限制访问）。"""
    db.reset_all(db.get_db_path())
    return {"status": "reset"}


# ---------- 冲突核对 ----------

class AdjudicateRequest(BaseModel):
    sample_no: str = Field(..., min_length=1, description="样品编号")
    test_item: str = Field(..., min_length=1, description="检测项目")
    unit: str = Field(..., min_length=1, description="单位")
    chosen_result_id: int = Field(..., description="可信记录的结果 ID")
    reason: str = Field(..., min_length=1, description="选择理由")
    reviewer: str = Field(..., min_length=1, description="质控员标识")
    record_ids: list[int] = Field(
        ..., description="核对时看到的组内全部结果 ID（来自组详情快照）"
    )


@app.get(
    "/review/groups",
    summary="冲突组列表：同一样品+项目+单位下结果值不一致的组",
)
def list_review_groups() -> dict[str, Any]:
    with db.connect() as conn:
        with db.read_snapshot(conn):
            groups = review.list_groups(conn)
    return {"count": len(groups), "groups": groups}


@app.get(
    "/review/group",
    summary="组详情快照：完整原始结果、邮件来源与当前裁决（同一数据快照）",
)
def get_review_group(sample_no: str, test_item: str, unit: str) -> dict[str, Any]:
    with db.connect() as conn:
        with db.read_snapshot(conn):
            detail = review.build_group_detail(conn, sample_no, test_item, unit)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"组合（样品={sample_no}，项目={test_item}，单位={unit}）"
                "没有任何检测结果"
            ),
        )
    return detail


@app.post(
    "/review/group/adjudicate",
    summary="提交可信选择（按结果 ID + 核对时看到的记录集合，过期提交拒绝）",
)
def adjudicate_group(req: AdjudicateRequest) -> dict[str, Any]:
    conn = db.connect()
    try:
        with db.write_transaction(conn):
            try:
                return review.adjudicate(
                    conn,
                    sample_no=req.sample_no,
                    test_item=req.test_item,
                    unit=req.unit,
                    chosen_result_id=req.chosen_result_id,
                    reason=req.reason,
                    reviewer=req.reviewer,
                    record_ids=req.record_ids,
                )
            except review.ReviewError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={
                        "error": exc.error,
                        "message": exc.message,
                        **exc.extra,
                    },
                ) from exc
    finally:
        conn.close()
