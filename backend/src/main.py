from datetime import datetime, timezone
import os
from .lease_desk import billing_routes, offer_routes
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from .db import init_db, query, one, execute
from .auth import current_user, require_roles, issue_token, same_password
from .schemas import LoginIn, ServiceEventIn, SurveyIn, ReviewIn
from .resident_routes import router as resident_router
from . import batches
from .services import audit, survey_rows

app = FastAPI(title="公寓租客续租因素分析")
app.include_router(resident_router)
app.include_router(offer_routes.router)
app.include_router(billing_routes.router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def startup():
    init_db()
    if os.getenv("SEED_DEMO", "true").lower() == "true":
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception as exc:
            print("seed skipped", exc, flush=True)
    # 首次启动留档一批分析，保证分析页与总览有可查看的批次；失败不影响服务
    try:
        if not one("SELECT id FROM analysis_batch ORDER BY id DESC LIMIT 1"):
            system = {"id": None, "email": "system@harborstay.local"}
            batches.create_batch(system)
    except Exception as exc:
        print("initial batch skipped", exc, flush=True)

@app.middleware("http")
async def audit_request(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Rental-Trace"] = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return response

@app.get("/health")
def health():
    row = one("SELECT 1 AS ok")
    return {"status":"healthy", "database": row["ok"] == 1}

@app.post("/auth/login")
def login(payload: LoginIn):
    user = one("SELECT id,email,full_name,role,password_hash FROM users WHERE email=%s", (payload.email,))
    if not user or not same_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    audit(user, "login", "user", user["id"], "登录成功")
    return {"access_token": issue_token(user), "user": {k:user[k] for k in ("id","email","full_name","role")}}

@app.get("/me")
def me(user=Depends(current_user)):
    return user

@app.get("/dashboard")
def dashboard(user=Depends(current_user)):
    return {
      "properties": one("SELECT COUNT(*) AS c FROM properties")["c"],
      "active_tenants": one("SELECT COUNT(*) AS c FROM tenants WHERE status='active'")["c"],
      "open_services": one("SELECT COUNT(*) AS c FROM service_events WHERE status='open'")["c"],
      "survey_count": one("SELECT COUNT(*) AS c FROM renewal_surveys")["c"],
      "followups": one("SELECT COUNT(*) AS c FROM follow_up_tasks WHERE status!='closed'")["c"],
      "avg_intent": one("SELECT COALESCE(AVG(renewal_intent),0) AS c FROM renewal_surveys")["c"]
    }

@app.get("/properties")
def properties(user=Depends(current_user)):
    return query("SELECT * FROM properties ORDER BY code")

@app.get("/tenants")
def tenants(user=Depends(current_user)):
    return query("SELECT t.*, p.code AS property_code, p.name AS property_name FROM tenants t JOIN properties p ON p.id=t.property_id ORDER BY t.status,t.full_name")

@app.get("/service-events")
def service_events(user=Depends(current_user)):
    return query("""SELECT se.*,t.full_name AS tenant_name,p.code AS property_code
      FROM service_events se JOIN tenants t ON t.id=se.tenant_id JOIN properties p ON p.id=se.property_id
      ORDER BY se.opened_at DESC""")

@app.post("/service-events")
def add_service_event(payload: ServiceEventIn, user=Depends(require_roles("service_coordinator", "customer_manager"))):
    tenant = one("SELECT id,property_id,status FROM tenants WHERE id=%s", (payload.tenant_id,))
    if not tenant:
        raise HTTPException(status_code=404, detail="租客不存在")
    if tenant["status"] == "moved_out":
        raise HTTPException(status_code=422, detail="已退租租客不能继续登记服务事件")
    opened = payload.opened_at or datetime.now(timezone.utc)
    resolved = datetime.now(timezone.utc) if payload.status == "resolved" else None
    duration = 1 if payload.status == "resolved" else None
    row = execute("""INSERT INTO service_events(tenant_id,property_id,category,severity,opened_at,resolved_at,resolution_days,satisfaction_score,status,summary)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (tenant["id"],tenant["property_id"],payload.category,payload.severity,opened,resolved,duration,payload.satisfaction_score,payload.status,payload.summary))
    audit(user,"create_service_event","service_event",row["id"],payload.category)
    return row

@app.get("/surveys")
def surveys(user=Depends(current_user)):
    return survey_rows()

@app.post("/surveys")
def add_survey(payload: SurveyIn, user=Depends(require_roles("analyst", "customer_manager"))):
    tenant = one("SELECT id,status FROM tenants WHERE id=%s", (payload.tenant_id,))
    if not tenant:
        raise HTTPException(status_code=404, detail="租客不存在")
    if tenant["status"] == "moved_out":
        raise HTTPException(status_code=422, detail="租客已退租，问卷不进入续租跟进")
    if one("SELECT id FROM renewal_surveys WHERE tenant_id=%s AND lease_period=%s", (payload.tenant_id,payload.lease_period)):
        raise HTTPException(status_code=409, detail="同一租客和租期已存在问卷")
    row = execute("""INSERT INTO renewal_surveys(tenant_id,lease_period,collected_at,cleanliness_score,service_score,value_score,community_score,renewal_intent,comments)
      VALUES(%s,%s,NOW(),%s,%s,%s,%s,%s,%s) RETURNING *""", (payload.tenant_id,payload.lease_period,payload.cleanliness_score,payload.service_score,payload.value_score,payload.community_score,payload.renewal_intent,payload.comments))
    audit(user,"collect_survey","renewal_survey",row["id"],payload.lease_period)
    return row

@app.get("/analysis/latest")
def analysis_latest(user=Depends(current_user)):
    # 始终读取最近一次成功分析批次的留档结果，不随实时数据漂移
    return batches.latest_batch()

@app.get("/analysis/batches")
def analysis_batches(user=Depends(current_user)):
    return batches.list_batches()

@app.get("/analysis/batches/{batch_id}")
def analysis_batch_detail(batch_id: int, user=Depends(current_user)):
    return batches.get_batch(batch_id)

@app.post("/analysis/run")
def analysis_run(user=Depends(require_roles("analyst", "customer_manager"))):
    # 成功才留档；分析失败抛 422，不产生批次，原批次仍可复核
    return batches.create_batch(user)

@app.get("/reviews/latest-preview")
def review_preview(user=Depends(current_user)):
    # 分析师只读查看预览；实际派单仍由 /reviews/approve 的经理鉴权把关
    return batches.latest_batch()

@app.post("/reviews/approve")
def approve_review(payload: ReviewIn, user=Depends(require_roles("customer_manager"))):
    # 无 batch_id 时复核最新批次；打开旧批次复核可显式带 batch_id
    batch_id = payload.batch_id or batches.latest_batch()["id"]
    return batches.approve_batch(batch_id, payload.note, user)

@app.get("/followups")
def followups(user=Depends(current_user)):
    return query("""SELECT f.*,t.full_name AS tenant_name,t.email,p.code AS property_code,p.name AS property_name
      FROM follow_up_tasks f JOIN tenants t ON t.id=f.tenant_id JOIN properties p ON p.id=t.property_id
      ORDER BY CASE f.priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, f.due_date""")

@app.post("/followups/{task_id}/close")
def close_followup(task_id: int, user=Depends(require_roles("customer_manager"))):
    task = one("SELECT id,status FROM follow_up_tasks WHERE id=%s", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="跟进任务不存在")
    execute("UPDATE follow_up_tasks SET status='closed' WHERE id=%s", (task_id,))
    audit(user,"close_follow_up","follow_up_task",task_id,"客户经理完成跟进")
    return {"id": task_id, "status": "closed"}

@app.get("/audit-logs")
def audit_logs(user=Depends(require_roles("customer_manager"))):
    return query("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 100")
