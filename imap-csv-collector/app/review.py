"""冲突核对：同一样品+检测项目+单位下数值不一致的结果组，由质控员选出可信记录。

设计要点：
  * 原始 test_result 永不修改，裁决单独存 adjudication 表；
  * 组详情在单个 SQLite 读事务（同一数据快照）里返回完整结果集与当前裁决；
  * 提交裁决必须带上核对时看到的记录 ID 集合（快照）：组内有新结果入库
    （快照过期）或别的质控员已先完成选择时，提交一律拒绝（409）；
  * 同组新结果入库时，采集事务会把该组 confirmed 裁决自动转为
    superseded（待复核），未核对的新值不会被标为已确认；
  * 非数值结果无法参与数值比对，一律标「待核实」供质控员人工判断。
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from . import db

STATUS_UNREVIEWED = "unreviewed"
STATUS_CONFIRMED = "confirmed"
STATUS_NEEDS_REVIEW = "needs_review"

STATUS_LABELS = {
    STATUS_UNREVIEWED: "未核对",
    STATUS_CONFIRMED: "已确认",
    STATUS_NEEDS_REVIEW: "待复核",
}

NON_NUMERIC_FLAG = "待核实"


class ReviewError(Exception):
    """核对流程中的业务拒绝，由路由层转成 HTTP 响应。"""

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.extra = extra or {}


def classify_value(text: str) -> float | None:
    """能解析为有限数值则返回数值，否则返回 None（非数值，标待核实）。"""
    try:
        value = float(text.strip())
    except (ValueError, AttributeError):
        return None
    return value if math.isfinite(value) else None


def _result_entry(row: sqlite3.Row, chosen_id: int | None) -> dict[str, Any]:
    numeric = classify_value(row["result"])
    return {
        "id": row["id"],
        "result": row["result"],
        "value_kind": "numeric" if numeric is not None else "non_numeric",
        "numeric_value": numeric,
        "review_flag": None if numeric is not None else NON_NUMERIC_FLAG,
        # 只有在「已确认」裁决下才标记可信记录；待复核/未核对时一律不标
        "is_chosen": chosen_id is not None and row["id"] == chosen_id,
        "source": {
            "mailbox_key": row["mailbox_key"],
            "message_id": row["message_id"],
            "uid": row["uid"],
            "ingested_at": row["ingested_at"],
        },
    }


def _adjudication_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chosen_result_id": row["chosen_result_id"],
        "reason": row["reason"],
        "reviewer": row["reviewer"],
        "record_ids": json.loads(row["record_ids"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
    }


def _group_status(
    confirmed: sqlite3.Row | None, latest_superseded: sqlite3.Row | None
) -> str:
    if confirmed is not None:
        return STATUS_CONFIRMED
    if latest_superseded is not None:
        return STATUS_NEEDS_REVIEW
    return STATUS_UNREVIEWED


def list_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """全部冲突组（同一样品+项目+单位下结果值不一致）及其裁决状态。"""
    groups: list[dict[str, Any]] = []
    for row in db.list_conflict_groups(conn):
        sample_no, test_item, unit = row["sample_no"], row["test_item"], row["unit"]
        results = db.get_group_results(conn, sample_no, test_item, unit)
        confirmed = db.get_confirmed_adjudication(conn, sample_no, test_item, unit)
        superseded = db.get_latest_superseded_adjudication(
            conn, sample_no, test_item, unit
        )
        status = _group_status(confirmed, superseded)
        groups.append(
            {
                "sample_no": sample_no,
                "test_item": test_item,
                "unit": unit,
                "result_count": len(results),
                "distinct_values": sorted({r["result"] for r in results}),
                "has_non_numeric": any(
                    classify_value(r["result"]) is None for r in results
                ),
                "record_ids": [r["id"] for r in results],
                "adjudication_status": status,
                "status_label": STATUS_LABELS[status],
            }
        )
    return groups


def build_group_detail(
    conn: sqlite3.Connection, sample_no: str, test_item: str, unit: str
) -> dict[str, Any] | None:
    """组详情快照：完整结果集（含邮件来源）+ 当前裁决，同一数据快照。

    调用方须把本函数放在 db.read_snapshot 里执行。
    """
    rows = db.get_group_results(conn, sample_no, test_item, unit)
    if not rows:
        return None
    confirmed = db.get_confirmed_adjudication(conn, sample_no, test_item, unit)
    superseded = db.get_latest_superseded_adjudication(
        conn, sample_no, test_item, unit
    )
    status = _group_status(confirmed, superseded)
    chosen_id = confirmed["chosen_result_id"] if confirmed is not None else None
    return {
        "group": {"sample_no": sample_no, "test_item": test_item, "unit": unit},
        "snapshot": {
            "record_ids": [row["id"] for row in rows],
            "record_count": len(rows),
        },
        "results": [_result_entry(row, chosen_id) for row in rows],
        "adjudication": {
            "status": status,
            "status_label": STATUS_LABELS[status],
            "current": _adjudication_entry(confirmed) if confirmed else None,
            "previous": _adjudication_entry(superseded) if superseded else None,
        },
    }


def adjudicate(
    conn: sqlite3.Connection,
    *,
    sample_no: str,
    test_item: str,
    unit: str,
    chosen_result_id: int,
    reason: str,
    reviewer: str,
    record_ids: list[int],
) -> dict[str, Any]:
    """提交可信选择。调用方须把本函数放在 db.write_transaction 里执行。

    拒绝情形（过期提交）：
      * 提交的记录集合与组内当前集合不一致（同组已有新结果入库）；
      * 该组已被别的质控员完成可信选择。
    """
    rows = db.get_group_results(conn, sample_no, test_item, unit)
    if not rows:
        raise ReviewError(
            404,
            "group_not_found",
            f"组合（样品={sample_no}，项目={test_item}，单位={unit}）"
            "没有任何检测结果",
        )
    current_ids = sorted(row["id"] for row in rows)
    submitted_ids = sorted(set(record_ids))
    if submitted_ids != current_ids:
        raise ReviewError(
            409,
            "stale_snapshot",
            "核对快照已过期：组内结果集合已变化（可能有新结果入库），"
            "请重新获取组详情后再提交",
            {"current_record_ids": current_ids},
        )
    if chosen_result_id not in current_ids:
        raise ReviewError(
            409,
            "chosen_not_in_group",
            f"结果 {chosen_result_id} 不属于该组，可信选择必须来自组内记录",
            {"current_record_ids": current_ids},
        )
    existing = db.get_confirmed_adjudication(conn, sample_no, test_item, unit)
    if existing is not None:
        raise ReviewError(
            409,
            "already_adjudicated",
            "该组已由其他质控员完成可信选择；同组新结果入库使其转为"
            "待复核后才能再次提交",
            {"adjudication": _adjudication_entry(existing)},
        )
    adjudication_id = db.insert_adjudication(
        conn,
        sample_no=sample_no,
        test_item=test_item,
        unit=unit,
        chosen_result_id=chosen_result_id,
        reason=reason,
        reviewer=reviewer,
        record_ids=current_ids,
    )
    created = db.get_adjudication(conn, adjudication_id)
    return {
        "status": STATUS_CONFIRMED,
        "status_label": STATUS_LABELS[STATUS_CONFIRMED],
        "group": {"sample_no": sample_no, "test_item": test_item, "unit": unit},
        "snapshot": {
            "record_ids": current_ids,
            "record_count": len(current_ids),
        },
        "adjudication": _adjudication_entry(created),
    }
