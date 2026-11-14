#!/usr/bin/env bash
# 端到端演示：docker compose up -d 后执行本脚本。
# 场景覆盖：
#   1. 多附件坏邮件 -> 停在该邮件、不留部分结果、返回邮件标识
#   2. 冲突核对：同一样品+项目+单位数值不一致 -> 快照查询、提交可信选择
#   3. 过期提交拒绝：别的质控员先完成 / 同组新结果入库使快照失效
#   4. 新邮件触发复核：已确认裁决自动转待复核，未核对新值不标已确认
#   5. 裁决状态重启后保留（重启 API 容器复查）
#   6. 原样重投 / 邮箱重建 / 附件被改 的采集语义保持不变
# 用法： bash scripts/demo.sh
set -euo pipefail

API="${API:-http://localhost:8000}"
CTL=(docker compose run --rm mailctl)

collect() {
  curl -sS -X POST "$API/collect" -H 'Content-Type: application/json' -d '{
    "host": "imap", "port": 10143, "folder": "INBOX",
    "username": "instrument@lab.local", "password": "labpass",
    "use_ssl": false
  }'
}

show() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))"; }
line() { printf '\n========== %s ==========\n' "$1"; }

# 冲突组：S-1001 / 铅Pb / mg/kg（仪器A 报 0.021，仪器B 报 0.026）
group_detail() {
  curl -sS -G "$API/review/group" \
    --data-urlencode "sample_no=S-1001" \
    --data-urlencode "test_item=铅Pb" \
    --data-urlencode "unit=mg/kg"
}

# 提交可信选择：$1=结果ID  $2=record_ids(JSON)  $3=质控员；打印 HTTP 状态码与响应体
post_adjudicate() {
  python3 - "$1" "$2" "$3" <<'PY' > /tmp/adjudicate.json
import json, sys
print(json.dumps({
    "sample_no": "S-1001", "test_item": "铅Pb", "unit": "mg/kg",
    "chosen_result_id": int(sys.argv[1]),
    "reason": "仪器A本周校准合格，复测均值与之一致，采信该值",
    "reviewer": sys.argv[3],
    "record_ids": json.loads(sys.argv[2]),
}, ensure_ascii=False))
PY
  curl -sS -o /tmp/adjudicate_resp.json -w 'HTTP %{http_code}\n' \
    -X POST "$API/review/group/adjudicate" \
    -H 'Content-Type: application/json' --data-binary @/tmp/adjudicate.json
  show < /tmp/adjudicate_resp.json
}

snapshot_ids() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['snapshot']['record_ids'], separators=(',',':')))"; }
first_result_id() { python3 -c "import json,sys; print(json.load(sys.stdin)['results'][0]['id'])"; }

line "1. 首轮采集：停在 UID3（两个 CSV 之一损坏），前两封共 6 条记录入库"
collect | show

line "2. 按样品查询 S-1001（含仪器A/B 两个铅Pb 值）；坏邮件里的 S-1005 未部分入库（404）"
curl -sS "$API/results/S-1001" | show
curl -sS -o /dev/null -w 'GET /results/S-1005 -> HTTP %{http_code}\n' "$API/results/S-1005"

line "3. 采集进度 last_uid=2，重启后从 UID3 续采"
curl -sS "$API/ingest/state" | show

line "4. 冲突核对：冲突组列表（同一样品+项目+单位，数值不一致）"
curl -sS "$API/review/groups" | show

line "5. 组详情快照：完整原始结果 + 邮件来源 + 当前裁决（同一数据快照）"
DETAIL=$(group_detail)
printf '%s' "$DETAIL" | show
CHOSEN=$(printf '%s' "$DETAIL" | first_result_id)
IDS=$(printf '%s' "$DETAIL" | snapshot_ids)

line "6. 质控员 qc-demo 提交可信选择（结果 ID=$CHOSEN + 核对时看到的记录集合 $IDS）"
post_adjudicate "$CHOSEN" "$IDS" "qc-demo"

line "7. 另一质控员基于同一快照重复提交 -> 409 already_adjudicated（先完成者优先）"
post_adjudicate "$CHOSEN" "$IDS" "qc-li"

line "8. 移除坏邮件 UID3，投递新邮件（同组第三个值：非数值「未检出」）后采集"
docker compose exec -T imap rm -f /srv/mail/INBOX/3.eml
cat > /tmp/batch_b4.csv <<'CSV'
样品编号,检测项目,结果,单位
S-1001,铅Pb,未检出,mg/kg
CSV
docker compose run --rm -v /tmp/batch_b4.csv:/tmp/batch_b4.csv mailctl \
  send --message-id inst-0004@lab-instrument --subject "仪器结果 批次 B4（复测）" \
  --attachment batch_b4.csv --csv-path /tmp/batch_b4.csv
echo "-> 采集响应中 superseded_adjudications=1：原裁决已自动转为待复核"
collect | show

line "9. 组详情：待复核；新值「未检出」标待核实，且任何记录都未标为已确认"
group_detail | show

line "10. 用旧快照 $IDS 再提交 -> 409 stale_snapshot（同组新结果入库，快照过期）"
post_adjudicate "$CHOSEN" "$IDS" "qc-li"

line "11. 重新获取快照后再提交 -> 已确认"
DETAIL=$(group_detail)
FRESH_IDS=$(printf '%s' "$DETAIL" | snapshot_ids)
post_adjudicate "$CHOSEN" "$FRESH_IDS" "qc-li"

line "12. 重启 API 容器，裁决状态重启后保留"
docker compose restart api >/dev/null
for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 1; done
group_detail | python3 -c "import json,sys; d=json.load(sys.stdin); a=d['adjudication']; print('裁决状态:', a['status_label'], '| 可信结果 ID:', a['current']['chosen_result_id'], '| 质控员:', a['current']['reviewer'])"

line "13. 原样重投 UID1 后采集：同一投递，跳过且不重复入库"
"${CTL[@]}" redeliver 1
collect | show

line "14. 邮箱重建（UIDVALIDITY 改变、UID 从 1 重排，内容不变）"
"${CTL[@]}" rebuild
"${CTL[@]}" list
line "14a. 重建后采集：整箱重扫，全部按重投跳过，零新增"
collect | show

line "15. 同 Message-ID 但附件被修改后重投：停采待核实，原结果保留"
cat > /tmp/changed.csv <<'CSV'
样品编号,检测项目,结果,单位
S-1001,铅Pb,9.999,mg/kg
CSV
docker compose run --rm -v /tmp/changed.csv:/tmp/changed.csv mailctl \
  redeliver-modified 1 \
  --attachment batch_b1.csv --csv-path /tmp/changed.csv
collect | show

line "16. 复查 S-1001：原始结果不变，伪造值 9.999 未入库"
curl -sS "$API/results/S-1001" | show

line "演示完成（可用 curl -X POST $API/admin/reset 重置结果库）"
