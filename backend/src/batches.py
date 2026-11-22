"""分析批次与批次复核。

每次成功的分析都会生成一个不可变的 analysis_batch：
- sample_snapshot：分析时刻的问卷样本（含当时的租客状态、租期、租金、服务实绩）
- analysis_result：偏相关 / 逐步回归计算结果
- followup_preview：按样本快照算出的跟进派单预览与跳过原因

经理复核只依据批次快照，不读实时表；复核整体在一个事务内完成，
失败回滚不留半批，重试幂等返回原结果，不重复派单。
"""
import json
from datetime import date, datetime
from decimal import Decimal

import psycopg
from fastapi import HTTPException

from .analysis import run_analysis
from .db import connect, one, query, execute
from .services import audit, survey_rows

BATCH_LIST_SQL = """SELECT b.id, b.created_at, b.created_by, b.sample_size, b.reviewed_at, b.review_note,
       u.full_name AS created_by_name, ru.full_name AS reviewed_by_name,
       (SELECT count(*) FROM jsonb_array_elements(b.followup_preview) AS item WHERE item->>'action'='dispatch') AS dispatch_count,
       (SELECT count(*) FROM jsonb_array_elements(b.followup_preview) AS item WHERE item->>'action'='skip') AS skip_count
FROM analysis_batch b
LEFT JOIN users u ON u.id=b.created_by
LEFT JOIN users ru ON ru.id=b.reviewed_by
ORDER BY b.id DESC"""


def _json_safe(value):
    """把 psycopg 行里的 date/Decimal 等转成可 JSON 序列化的值。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row_to_json(row):
    return {key: _json_safe(value) for key, value in row.items()}


def _priority(intent, service_score):
    return "high" if intent <= 4 or service_score <= 2 else "medium"


def build_preview(sample_rows):
    """按分析时刻的样本快照生成跟进预览，跳过项必须给出原因。"""
    preview = []
    for s in sample_rows:
        tenant_id = s["tenant_id"]
        base = {
            "survey_id": s["id"],
            "tenant_id": tenant_id,
            "tenant_name": s["tenant_name"],
            "property_code": s["property_code"],
            "lease_period": s["lease_period"],
            "lease_end_snapshot": _json_safe(s["lease_end_date"]),
            "renewal_intent": _json_safe(s["renewal_intent"]),
            "service_score": _json_safe(s["service_score"]),
            "tenant_status_snapshot": s["tenant_status"],
        }
        if s["tenant_status"] != "active":
            preview.append({**base, "action": "skip", "skip_reason": "快照时租客已退租，不生成续租跟进"})
        elif s["renewal_intent"] >= 7 and s["service_score"] >= 4:
            preview.append({**base, "action": "skip", "skip_reason": "续租意愿与服务评分均达标，无需介入"})
        else:
            reason = f"续租意愿 {s['renewal_intent']}/10，服务评分 {s['service_score']}/5"
            preview.append({
                **base,
                "action": "dispatch",
                "priority": _priority(s["renewal_intent"], s["service_score"]),
                "reason": reason,
            })
    return preview


def create_batch(user):
    """运行分析并把样本、结果、预览整批落档；分析失败不产生批次。"""
    sample_rows = survey_rows()
    try:
        result = run_analysis(sample_rows)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    snapshot = [_row_to_json(row) for row in sample_rows]
    preview = build_preview(sample_rows)
    batch = one(
        """INSERT INTO analysis_batch(created_by,sample_size,sample_snapshot,analysis_result,followup_preview)
           VALUES(%s,%s,%s,%s,%s) RETURNING id""",
        (user["id"], len(snapshot), json.dumps(snapshot),
         json.dumps(result), json.dumps(preview)),
    )
    # 保持与旧流程一致：成功分析后标记样本已分析
    execute("UPDATE renewal_surveys SET analyzed_at=NOW() WHERE analyzed_at IS NULL")
    audit(user, "run_analysis", "analysis_batch", batch["id"],
          f"批次 {batch['id']}：n={result['sample_size']}; partial={result['partial_correlation']}")
    return get_batch(batch["id"])


def list_batches():
    return query(BATCH_LIST_SQL)


def get_batch(batch_id):
    batch = one("SELECT * FROM analysis_batch WHERE id=%s", (batch_id,))
    if not batch:
        raise HTTPException(status_code=404, detail="分析批次不存在")
    latest = one("SELECT id FROM analysis_batch ORDER BY id DESC LIMIT 1")
    result = batch["analysis_result"]
    return {
        "id": batch["id"],
        "created_at": batch["created_at"],
        "created_by": batch["created_by"],
        "sample_size": batch["sample_size"],
        "reviewed_at": batch["reviewed_at"],
        "review_note": batch["review_note"],
        "is_latest": latest["id"] == batch["id"],
        "sample_snapshot": batch["sample_snapshot"],
        "analysis_result": result,
        "followup_preview": batch["followup_preview"],
        # 扁平字段，兼容旧的模型摘要视图
        "partial_correlation": result["partial_correlation"],
        "partial_corr_label": result["partial_corr_label"],
        "stepwise": result["stepwise"],
        "explanation": result["explanation"],
    }


def latest_batch():
    batch = one("SELECT id FROM analysis_batch ORDER BY id DESC LIMIT 1")
    if not batch:
        raise HTTPException(status_code=422, detail="尚无成功分析批次，请先运行分析")
    return get_batch(batch["id"])


def approve_batch(batch_id, note, user):
    """按批次预览派单。整个复核在一个事务内：任何失败整体回滚，不留半批跟进。"""
    with connect() as conn:
        try:
            batch = conn.execute("SELECT * FROM analysis_batch WHERE id=%s FOR UPDATE", (batch_id,)).fetchone()
            if not batch:
                raise HTTPException(status_code=404, detail="分析批次不存在")

            # 已办成的复核重试：回原结果，不再派单
            if batch["reviewed_at"] is not None:
                existing = conn.execute(
                    "SELECT id FROM follow_up_tasks WHERE batch_id=%s ORDER BY id", (batch_id,)).fetchall()
                conn.execute(
                    "INSERT INTO audit_logs(actor_email,action,entity_type,entity_id,detail) VALUES(%s,%s,%s,%s,%s)",
                    (user["email"], "retry_review_hit_original", "analysis_batch", batch_id,
                     f"批次 {batch_id} 已复核，返回原结果，跟进 {len(existing)} 条"))
                conn.commit()
                return {
                    "batch_id": batch_id,
                    "idempotent_replay": True,
                    "created_followups": [row["id"] for row in existing],
                    "skipped": [item for item in batch["followup_preview"] if item["action"] == "skip"],
                    "note": batch["review_note"],
                }

            latest = conn.execute("SELECT id FROM analysis_batch ORDER BY id DESC LIMIT 1").fetchone()
            if latest["id"] != batch_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"该批次已被更新的批次 {latest['id']} 替换，请重新打开最新批次后再复核")

            created = []
            skipped = []
            for item in batch["followup_preview"]:
                if item["action"] != "dispatch":
                    skipped.append(item)
                    continue
                # 复核时刻再核对实时状态：已退租或租期变了的列出跳过原因，不派单
                tenant = conn.execute(
                    "SELECT id,status,lease_end_date FROM tenants WHERE id=%s FOR UPDATE",
                    (item["tenant_id"],)).fetchone()
                if not tenant:
                    skipped.append({**item, "action": "skip", "skip_reason": "租客档案已不存在"})
                    continue
                if tenant["status"] != "active":
                    status_text = "已退租" if tenant["status"] == "moved_out" else tenant["status"]
                    skipped.append({**item, "action": "skip",
                                    "skip_reason": f"复核时租客{status_text}（快照时在住），跳过派单"})
                    continue
                if tenant["lease_end_date"].isoformat() != item["lease_end_snapshot"]:
                    skipped.append({
                        **item, "action": "skip",
                        "skip_reason": (f"租期已变化：快照租期止于 {item['lease_end_snapshot']}，"
                                        f"当前止于 {tenant['lease_end_date'].isoformat()}，跳过派单"),
                    })
                    continue
                survey = conn.execute("SELECT id FROM renewal_surveys WHERE id=%s", (item["survey_id"],)).fetchone()
                if not survey:
                    skipped.append({**item, "action": "skip", "skip_reason": "问卷样本已被移除，跳过派单"})
                    continue
                # follow_up_tasks(survey_id) 全局唯一：该问卷已有跟进（含历史无批次任务）则不重复派单
                already = conn.execute(
                    "SELECT id, batch_id FROM follow_up_tasks WHERE survey_id=%s", (item["survey_id"],)).fetchone()
                if already:
                    skipped.append({**item, "action": "skip",
                                    "skip_reason": f"该问卷已有跟进任务 #{already['id']}，不重复派单"})
                    continue
                task = None
                conn.execute("SAVEPOINT followup_insert")
                try:
                    task = conn.execute(
                        """INSERT INTO follow_up_tasks(batch_id,tenant_id,survey_id,task_type,priority,owner,due_date,reason,lease_end_snapshot)
                           VALUES(%s,%s,%s,'续租沟通',%s,'customer_manager',CURRENT_DATE + INTERVAL '5 days',%s,%s)
                           ON CONFLICT (batch_id, survey_id) DO NOTHING
                           RETURNING id""",
                        (batch_id, item["tenant_id"], item["survey_id"], item["priority"],
                         item["reason"], item["lease_end_snapshot"])).fetchone()
                except psycopg.errors.UniqueViolation:
                    # 并发复核中其他事务已为该问卷派单（survey_id 全局唯一）：回滚到保存点并跳过
                    conn.execute("ROLLBACK TO SAVEPOINT followup_insert")
                    skipped.append({**item, "action": "skip", "skip_reason": "该问卷刚被其他复核派单，不重复派单"})
                    continue
                if task:
                    created.append(task["id"])
                    conn.execute(
                        "INSERT INTO audit_logs(actor_email,action,entity_type,entity_id,detail) VALUES(%s,%s,%s,%s,%s)",
                        (user["email"], "create_follow_up", "follow_up_task", task["id"], item["reason"]))

            conn.execute(
                "UPDATE analysis_batch SET reviewed_by=%s, reviewed_at=NOW(), review_note=%s WHERE id=%s",
                (user["id"], note, batch_id))
            conn.execute(
                """UPDATE renewal_surveys SET status='reviewed', reviewed_at=NOW()
                   WHERE id = ANY(%s) AND status='submitted'""",
                ([item["survey_id"] for item in batch["followup_preview"]],))
            conn.execute(
                "INSERT INTO audit_logs(actor_email,action,entity_type,entity_id,detail) VALUES(%s,%s,%s,%s,%s)",
                (user["email"], "approve_review", "analysis_batch", batch_id,
                 f"批次 {batch_id}：派单 {len(created)} 条，跳过 {len(skipped)} 条；{note}"))
            conn.commit()
            return {
                "batch_id": batch_id,
                "idempotent_replay": False,
                "created_followups": created,
                "skipped": skipped,
                "note": note,
            }
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
