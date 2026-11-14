# 仪器邮件 CSV 采集服务

检测中心资料员无需逐封下载邮件：服务通过 IMAP 自动采集仪器邮件中的 CSV 附件，
解析后写入 SQLite 样品结果库，并提供按样品编号查询的 HTTP API。

质控员可核对「同一样品 + 检测项目 + 单位」下数值不一致的冲突结果组：
在同一数据快照中查看全部原始结果与邮件来源，按结果 ID + 核对时看到的记录
集合提交可信选择与理由；原始数据永不修改。同组新结果入库后原裁决自动转为
待复核，过期提交（快照失效 / 他人已先完成）一律拒绝。

## 核心保证

| 需求 | 实现方式 |
| --- | --- |
| 整封邮件原子入库 | 一封邮件的**全部** CSV 解析成功后，检测记录、投递记录、UID 进度才在同一个 SQLite 事务提交；任一附件损坏则整封放弃，无部分结果 |
| 失败停在该邮件并返回标识 | 坏附件 / 断线 / 冲突都通过响应的 `stopped` 字段返回 UID、Message-ID、主题和原因，进度不推进 |
| 重启可续采 | 进度持久化在 `ingest_state`，下次从 `last_uid + 1` 开始 |
| UIDVALIDITY 改变后重扫 | 检测到变化即清零进度重新扫描，历史投递记录保留用于去重 |
| 重投识别 | Message-ID + 全部附件的 SHA-256 摘要：同 ID 同摘要跳过；同 ID 不同摘要停采待核实，原结果原样保留 |
| 采集参数 | 每次采集请求显式传入 IMAP 主机、端口、文件夹及账号密码 |
| 原始数据不可变 | 裁决单独存 `adjudication` 表，`test_result` 写入后永不修改 |
| 同一数据快照查询 | 组详情在单个 SQLite 读事务里返回完整结果集与当前裁决，不会新旧混杂 |
| 过期提交拒绝 | 提交必须带核对时看到的记录 ID 集合；同组新结果入库（快照失效）或别的质控员已先完成选择时返回 409 |
| 新结果触发复核 | 同组新结果入库的**同一事务**里把该组已确认裁决转为待复核；未核对的新值不会被标为已确认 |
| 非数值结果待核实 | 不能解析为有限数值的结果在核对视图中标 `待核实` |
| 裁决持久化 | 裁决存于 SQLite，服务重启后保留 |

## 目录结构

```
app/                 FastAPI + 采集核心 + SQLite 持久层
  main.py            HTTP 路由（采集 / 查询 / 冲突核对）
  collector.py       IMAP 采集、重投识别、事务边界（含裁决自动失效）
  review.py          冲突核对：快照视图、可信选择、过期拒绝
  csv_parse.py       CSV 校验解析（表头：样品编号,检测项目,结果,单位）
  db.py              SQLite 建表与查询
imap_server/         本地 IMAP 样例服务（纯标准库实现 IMAP4rev1 子集）
  server.py          IMAP 协议服务，支持 FLAKY_FETCH_AFTER 断线注入
  mailbox.py         文件邮箱、附件替换、邮箱重建
  seed.py            演示种子：两封正常邮件（含一组数值冲突）+ 一封坏 CSV 邮件
  cli.py             邮箱控制 CLI（投递/重投/重建/修复）
tests/               19 个 pytest 用例，覆盖全部承诺语义
scripts/demo.sh      端到端演示脚本
docker-compose.yml   api + imap 两个服务 + mailctl 控制容器
```

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

- API 文档： http://localhost:8000/docs
- IMAP 样例服务： localhost:10143（明文 IMAP，账号 `instrument@lab.local` / `labpass`）
- SQLite 数据与邮箱分别持久化在 `db_data`、`mail_data` 卷

运行完整演示（多附件失败、冲突核对、新邮件触发复核、重投、邮箱重建、冲突停采）：

```bash
bash scripts/demo.sh
```

### 断线场景演示

给 imap 服务注入故障：第 N 次 FETCH 响应发送一半即断开 TCP。

```bash
FLAKY_FETCH_AFTER=2 docker compose up -d --force-recreate imap
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{
  "host":"imap","port":10143,"folder":"INBOX",
  "username":"instrument@lab.local","password":"labpass","use_ssl":false}'
# stopped.uid 指向失败邮件；去掉环境变量重启 imap 后再次调用即从断点续采
```

## API

### POST /collect — 触发一轮采集

```bash
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' -d '{
  "host": "imap",
  "port": 10143,
  "folder": "INBOX",
  "username": "instrument@lab.local",
  "password": "labpass",
  "use_ssl": false
}'
```

响应（正常完成 `status=completed`；停采时 `status=stopped` 并带 `stopped` 邮件标识）：

```json
{
  "status": "stopped",
  "mailbox_key": "imap|10143|INBOX",
  "uidvalidity": 1794581572,
  "resume_from_uid": 3,
  "scanned_uids": [1, 2, 3],
  "ingested": [ {"uid": 1, "message_id": "inst-0001@lab-instrument", "record_count": 3, "...": "..."} ],
  "skipped_duplicates": [],
  "stopped": {
    "reason": "坏 CSV 附件，整封邮件不入库: 附件 batch_b3_bad.csv: 第 3 行结果为空",
    "uid": 3,
    "message_id": "inst-0003@lab-instrument",
    "subject": "仪器结果 批次 B3（含损坏附件）"
  },
  "total_new_records": 5
}
```

### GET /results/{样品编号} — 按样品查询

```bash
curl localhost:8000/results/S-1001
```

### 其他端点

| 端点 | 说明 |
| --- | --- |
| `GET /samples` | 已入库样品汇总 |
| `GET /ingest/state` | 各邮箱 UIDVALIDITY 与 last_uid 进度 |
| `GET /ingest/deliveries` | 已处理投递（Message-ID、附件摘要） |
| `GET /health` | 健康检查 |
| `POST /admin/reset` | 清空结果库（演示用） |

## 冲突核对 API

冲突组 = 同一样品编号 + 检测项目 + 单位下出现两个及以上不同结果值。
裁决状态：`unreviewed`（未核对）→ `confirmed`（已确认）→ 同组新结果入库后
自动转为 `needs_review`（待复核），重新提交后再次 `confirmed`。

### GET /review/groups — 冲突组列表

```bash
curl localhost:8000/review/groups
```

### GET /review/group — 组详情快照

```bash
curl -G localhost:8000/review/group \
  --data-urlencode 'sample_no=S-1001' \
  --data-urlencode 'test_item=铅Pb' \
  --data-urlencode 'unit=mg/kg'
```

同一数据快照返回：组内全部原始结果（含结果 ID、数值解析、`待核实` 标记）、
邮件来源（Message-ID / UID / 邮箱）、当前裁决，以及提交裁决要带的
`snapshot.record_ids`。

### POST /review/group/adjudicate — 提交可信选择

```bash
curl -X POST localhost:8000/review/group/adjudicate \
  -H 'Content-Type: application/json' -d '{
    "sample_no": "S-1001", "test_item": "铅Pb", "unit": "mg/kg",
    "chosen_result_id": 1,
    "reason": "仪器A本周校准合格，复测均值与之一致",
    "reviewer": "qc-wang",
    "record_ids": [1, 4]
  }'
```

- `chosen_result_id` 必须属于该组；`record_ids` 必须与组内当前结果集合一致。
- 过期提交返回 409：`stale_snapshot`（同组已有新结果入库）、
  `already_adjudicated`（别的质控员已先完成）、`chosen_not_in_group`。
- 同组新结果入库后，原裁决自动转为待复核：组详情里 `adjudication.previous`
  保留原选择备查，但所有记录（含未核对的新值）都不会标为已确认，
  需用新快照重新提交。

### /docs 演示路径（冲突核对 + 新邮件触发复核）

1. `docker compose up -d --build` 后打开 http://localhost:8000/docs
2. `POST /collect`（host=`imap`，port=`10143`，folder=`INBOX`，
   账号 `instrument@lab.local` / `labpass`）：入库前两封邮件，
   S-1001 铅Pb 出现 0.021 / 0.026 冲突（停在坏邮件 UID3 属预期）
3. `GET /review/groups` 看到冲突组 → `GET /review/group` 取快照
4. `POST /review/group/adjudicate` 用快照里的 `record_ids` 提交可信选择
   → `confirmed`；换个 `reviewer` 再提交同一快照 → 409
5. 触发新邮件（命令行执行一次）：
   ```bash
   docker compose exec -T imap rm -f /srv/mail/INBOX/3.eml   # 移除坏邮件
   printf '样品编号,检测项目,结果,单位\nS-1001,铅Pb,未检出,mg/kg\n' > /tmp/b4.csv
   docker compose run --rm -v /tmp/b4.csv:/tmp/b4.csv mailctl \
     send --message-id inst-0004@lab-instrument \
     --attachment batch_b4.csv --csv-path /tmp/b4.csv
   ```
6. 回到 /docs 再调 `POST /collect`：响应里
   `ingested[0].superseded_adjudications=1`，原裁决已自动转待复核
7. `GET /review/group`：状态 `needs_review`，新值「未检出」标 `待核实`，
   无任何记录标为已确认；用旧 `record_ids` 提交 → 409 `stale_snapshot`；
   取新快照重新提交 → 再次 `confirmed`

## CSV 约定

UTF-8（可带 BOM），首行表头固定为 `样品编号,检测项目,结果,单位`，
四个字段均不允许为空，否则该附件所在整封邮件判定失败。

## 本地无 Docker 运行 / 测试

```bash
python -m venv .venv && source .venv/bin/activate   # 或使用已有解释器
pip install -r requirements.txt pytest httpx
PYTHONPATH=. pytest -q

# 手动起两个进程
python -m imap_server.cli serve --host 127.0.0.1 --port 10143
COLLECTOR_DB=./data/collector.db uvicorn app.main:app --port 8000
```

## 设计说明与边界

- 采集按邮箱文件夹维度维护进度，`mailbox_key = host|port|folder`。
- 附件摘要对「文件名 + 文件内容 SHA-256」排序后再做一次 SHA-256，
  附件顺序变化不影响判定，内容或文件名变化即视为不同投递。
- 样例 IMAP 服务仅实现采集所需命令（LOGIN/SELECT/STATUS/UID SEARCH/UID FETCH 等），
  面向演示与测试，不可作为生产邮箱使用。
- `/collect` 在单进程内加锁串行化；生产多副本部署时应改用分布式锁或单 worker。
- 裁决提交用 `BEGIN IMMEDIATE` 写事务串行化「读快照 -> 校验 -> 写裁决」，
  并有「每组仅一条 confirmed」的部分唯一索引兜底；采集写入与裁决失效在
  同一事务提交，两者不会交错出中间态。
- 数值判定为 `float` 可解析且有限（`nan`/`inf` 视为非数值）；非数值结果
  不参与数值比较，仅标 `待核实` 供人工判断，不阻止质控员采信。
