"""冲突核对 API 测试：快照查询、可信选择、过期拒绝、新结果触发复核。"""
import os
import threading

from fastapi.testclient import TestClient

from app import db, review
from app.main import app
from imap_server import mailbox
from tests.conftest import PASSWORD, USER

GROUP = {"sample_no": "S-1001", "test_item": "铅Pb", "unit": "mg/kg"}


def _collect(client, host, port):
    resp = client.post(
        "/collect",
        json={
            "host": host,
            "port": port,
            "folder": "INBOX",
            "username": USER,
            "password": PASSWORD,
            "use_ssl": False,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _detail(client, **group):
    resp = client.get("/review/group", params=group or GROUP)
    assert resp.status_code == 200
    return resp.json()


def _adjudicate(client, chosen, record_ids, reviewer="qc-wang", **group):
    return client.post(
        "/review/group/adjudicate",
        json={
            **(group or GROUP),
            "chosen_result_id": chosen,
            "reason": "仪器A本周校准合格，复测均值与之一致",
            "reviewer": reviewer,
            "record_ids": record_ids,
        },
    )


def _seed_rows(db_path, records, message_id="m1@x", uid=1):
    with db.connect(db_path) as conn:
        db.init_db(conn)
        db.insert_results(conn, "mbox|10143|INBOX", message_id, uid, records)
        conn.commit()


def test_conflict_review_end_to_end(imap_server, db_path):
    host, port, _root = imap_server
    client = TestClient(app)
    report = _collect(client, host, port)
    assert report["stopped"]["uid"] == 3  # 坏邮件挡住后续，前两封已入库

    # 冲突组列表：S-1001 铅Pb 出现 0.021 / 0.026 两个值
    groups = client.get("/review/groups").json()["groups"]
    group = next(
        g for g in groups if g["sample_no"] == "S-1001" and g["test_item"] == "铅Pb"
    )
    assert group["adjudication_status"] == "unreviewed"
    assert group["status_label"] == "未核对"
    assert group["result_count"] == 2
    assert set(group["distinct_values"]) == {"0.021", "0.026"}
    assert group["has_non_numeric"] is False

    # 组详情快照：完整结果 + 邮件来源 + 当前裁决
    detail = _detail(client)
    assert detail["adjudication"]["status"] == "unreviewed"
    assert detail["adjudication"]["current"] is None
    ids = detail["snapshot"]["record_ids"]
    assert len(ids) == 2
    sources = {r["source"]["message_id"] for r in detail["results"]}
    assert sources == {"inst-0001@lab-instrument", "inst-0002@lab-instrument"}
    assert all(r["source"]["uid"] for r in detail["results"])
    assert all(r["is_chosen"] is False for r in detail["results"])

    # 提交可信选择 -> 已确认
    chosen = ids[0]
    resp = _adjudicate(client, chosen, ids)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["adjudication"]["chosen_result_id"] == chosen
    assert body["adjudication"]["record_ids"] == ids

    detail = _detail(client)
    assert detail["adjudication"]["status"] == "confirmed"
    chosen_flags = {r["id"]: r["is_chosen"] for r in detail["results"]}
    assert chosen_flags == {ids[0]: True, ids[1]: False}

    # 别的质控员基于同一快照再提交 -> 拒绝（先完成者优先）
    resp = _adjudicate(client, ids[1], ids, reviewer="qc-li")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "already_adjudicated"

    # 原始数据保持不变
    results = client.get("/results/S-1001").json()["results"]
    assert sorted(r["result"] for r in results) == ["0.008", "0.021", "0.026"]


def test_new_result_supersedes_and_stale_rejected(imap_server, db_path):
    host, port, root = imap_server
    client = TestClient(app)
    _collect(client, host, port)

    detail = _detail(client)
    ids = detail["snapshot"]["record_ids"]
    chosen = ids[0]
    assert _adjudicate(client, chosen, ids).status_code == 200

    # 同组新结果随新邮件入库（非数值「未检出」），坏邮件先移除让采集通过
    os.remove(f"{mailbox.folder_dir(root, 'INBOX')}/3.eml")
    new_mail = mailbox.build_message(
        message_id="inst-0004@lab-instrument",
        subject="仪器结果 批次 B4（复测）",
        sender="instrument-c@lab.local",
        recipient="lab@lab.local",
        attachments=[
            (
                "batch_b4.csv",
                (
                    "样品编号,检测项目,结果,单位\r\n"
                    "S-1001,铅Pb,未检出,mg/kg\r\n"
                ).encode("utf-8"),
            )
        ],
    )
    mailbox.append_message(root, "INBOX", new_mail)
    report = _collect(client, host, port)
    assert report["stopped"] is None
    assert report["ingested"][0]["superseded_adjudications"] == 1

    # 旧快照提交 -> 过期拒绝
    resp = _adjudicate(client, chosen, ids, reviewer="qc-li")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "stale_snapshot"

    # 组详情：待复核；新值未核对不得标为已确认；非数值标待核实
    detail = _detail(client)
    assert detail["adjudication"]["status"] == "needs_review"
    assert detail["adjudication"]["status_label"] == "待复核"
    assert detail["adjudication"]["current"] is None
    assert detail["adjudication"]["previous"]["chosen_result_id"] == chosen
    assert detail["snapshot"]["record_count"] == 3
    assert all(r["is_chosen"] is False for r in detail["results"])
    new_value = next(r for r in detail["results"] if r["result"] == "未检出")
    assert new_value["value_kind"] == "non_numeric"
    assert new_value["review_flag"] == "待核实"

    # 采集期间仍可核对：用新快照重新裁决 -> 已确认
    fresh_ids = detail["snapshot"]["record_ids"]
    resp = _adjudicate(client, chosen, fresh_ids, reviewer="qc-li")
    assert resp.status_code == 200
    detail = _detail(client)
    assert detail["adjudication"]["status"] == "confirmed"
    chosen_flags = {r["id"]: r["is_chosen"] for r in detail["results"]}
    assert chosen_flags[chosen] is True
    assert chosen_flags[new_value["id"]] is False

    # 原始数据保持四行，裁决不改写原始结果
    results = client.get("/results/S-1001").json()["results"]
    assert sorted(r["result"] for r in results) == ["0.008", "0.021", "0.026", "未检出"]


def test_adjudicate_validation_and_group_not_found(db_path):
    client = TestClient(app)
    _seed_rows(
        db_path,
        [("S-9", "pH", "7.1", "无量纲"), ("S-9", "pH", "偏酸", "无量纲")],
    )
    group = {"sample_no": "S-9", "test_item": "pH", "unit": "无量纲"}

    # 非数值结果标待核实，数值结果给出 numeric_value
    detail = _detail(client, **group)
    flags = {r["result"]: r["review_flag"] for r in detail["results"]}
    assert flags == {"7.1": None, "偏酸": "待核实"}
    numeric = {r["result"]: r["numeric_value"] for r in detail["results"]}
    assert numeric["7.1"] == 7.1 and numeric["偏酸"] is None

    # 可信选择不在组内 -> 拒绝
    ids = detail["snapshot"]["record_ids"]
    resp = _adjudicate(client, 99999, ids, **group)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "chosen_not_in_group"

    # 组不存在 -> 404
    missing = {"sample_no": "X", "test_item": "Y", "unit": "Z"}
    resp = client.get("/review/group", params=missing)
    assert resp.status_code == 404
    resp = _adjudicate(client, 1, [1], **missing)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "group_not_found"


def test_identical_values_not_listed_as_conflict(db_path):
    client = TestClient(app)
    _seed_rows(db_path, [("S-8", "汞Hg", "0.01", "mg/kg")], message_id="m1@x", uid=1)
    _seed_rows(db_path, [("S-8", "汞Hg", "0.01", "mg/kg")], message_id="m2@x", uid=2)

    groups = client.get("/review/groups").json()
    assert groups["count"] == 0
    # 非冲突组仍可查看详情（状态：未核对）
    detail = _detail(client, sample_no="S-8", test_item="汞Hg", unit="mg/kg")
    assert detail["snapshot"]["record_count"] == 2
    assert detail["adjudication"]["status"] == "unreviewed"


def test_adjudication_persists_across_restart(db_path):
    client = TestClient(app)
    _seed_rows(
        db_path,
        [("S-9", "pH", "7.1", "无量纲"), ("S-9", "pH", "7.4", "无量纲")],
    )
    group = {"sample_no": "S-9", "test_item": "pH", "unit": "无量纲"}
    detail = _detail(client, **group)
    ids = detail["snapshot"]["record_ids"]
    assert _adjudicate(client, ids[0], ids, **group).status_code == 200

    # 模拟服务重启：全新连接 + 全新 TestClient，裁决仍在
    with db.connect(db_path) as conn:
        confirmed = db.get_confirmed_adjudication(conn, "S-9", "pH", "无量纲")
        assert confirmed is not None
        assert confirmed["chosen_result_id"] == ids[0]
    fresh_client = TestClient(app)
    detail = _detail(fresh_client, **group)
    assert detail["adjudication"]["status"] == "confirmed"
    assert detail["adjudication"]["current"]["reviewer"] == "qc-wang"


def test_concurrent_adjudicate_only_one_wins(db_path):
    _seed_rows(
        db_path,
        [("S-7", "镉Cd", "0.01", "mg/kg"), ("S-7", "镉Cd", "0.02", "mg/kg")],
    )
    with db.connect(db_path) as conn:
        ids = [
            row["id"] for row in db.get_group_results(conn, "S-7", "镉Cd", "mg/kg")
        ]

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def worker(reviewer):
        conn = db.connect(db_path)  # 每个线程自己的连接
        try:
            barrier.wait()
            with db.write_transaction(conn):
                review.adjudicate(
                    conn,
                    sample_no="S-7",
                    test_item="镉Cd",
                    unit="mg/kg",
                    chosen_result_id=ids[0],
                    reason="并发核对测试",
                    reviewer=reviewer,
                    record_ids=ids,
                )
            outcome = "ok"
        except review.ReviewError as exc:
            outcome = exc.error
        finally:
            conn.close()
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=worker, args=(f"qc-{i}",)) for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 恰好一个成功，另一个被「已先完成」拒绝；不会双确认
    assert sorted(outcomes) == ["already_adjudicated", "ok"]
    with db.connect(db_path) as conn:
        confirmed = db.get_confirmed_adjudication(conn, "S-7", "镉Cd", "mg/kg")
        assert confirmed is not None
