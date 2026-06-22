import csv
import io
from datetime import datetime, date, timedelta
from typing import List, Optional
from io import StringIO

from fastapi import FastAPI, Depends, Request, Form, HTTPException, Response, Cookie, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from database import get_db, engine, Base, SessionLocal
import models
import inventory as inv
from auth import (
    hash_password, verify_password, create_session, get_current_user,
    clear_session, get_session
)


Base.metadata.create_all(bind=engine)

app = FastAPI(title="校园实验耗材领用审批系统")

CATEGORY_MAP = {
    "reagent": "试剂",
    "glove": "手套",
    "dish": "培养皿",
    "other": "其他"
}

RISK_MAP = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险"
}

ROLE_MAP = {
    "teacher": "教师",
    "keeper": "库管员",
    "director": "系主任"
}

STATUS_MAP = {
    "pending": "待审批",
    "approved": "已批准（待发放）",
    "partially_approved": "部分批准",
    "rejected": "已驳回",
    "completed": "已完成（全部发放）"
}

URGENCY_MAP = {
    "normal": "普通",
    "urgent": "加急",
    "emergency": "紧急"
}

TXN_TYPE_MAP = {
    "in": "入库",
    "out": "出库",
    "freeze": "冻结",
    "unfreeze": "解冻",
    "adjust": "调整"
}

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals.update({
    "CATEGORY_MAP": CATEGORY_MAP,
    "RISK_MAP": RISK_MAP,
    "ROLE_MAP": ROLE_MAP,
    "STATUS_MAP": STATUS_MAP,
    "URGENCY_MAP": URGENCY_MAP,
    "TXN_TYPE_MAP": TXN_TYPE_MAP,
    "is_batch_expired": inv.is_batch_expired,
    "today": date.today(),
})


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code in (302, 303, 307, 308):
        location = exc.headers.get("Location", "/") if exc.headers else "/"
        return RedirectResponse(url=location, status_code=303)
    return await http_exception_handler(request, exc)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "", session_id: Optional[str] = Cookie(default=None)):
    if session_id and get_session(session_id):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "next": next})


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not verify_password(password, user.password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "用户名或密码错误",
            "next": next,
            "username": username
        })
    session_id = create_session(user.id)
    redirect_url = next or "/"
    resp = RedirectResponse(redirect_url, status_code=303)
    resp.set_cookie(key="session_id", value=session_id, httponly=True)
    return resp


@app.post("/logout")
async def logout(session_id: Optional[str] = Cookie(default=None)):
    if session_id:
        clear_session(session_id)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session_id")
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    stats = {}
    if current_user.role == "teacher":
        my_requests = db.query(models.SupplyRequest).filter(
            models.SupplyRequest.applicant_id == current_user.id
        ).all()
        stats["total_requests"] = len(my_requests)
        stats["pending"] = sum(1 for r in my_requests if r.status == "pending")
        stats["approved"] = sum(1 for r in my_requests if r.status in ("approved", "partially_approved"))
        stats["rejected"] = sum(1 for r in my_requests if r.status == "rejected")
        my_plans = db.query(models.ExperimentPlan).filter(
            models.ExperimentPlan.creator_id == current_user.id
        ).all()
        stats["total_plans"] = len(my_plans)
    elif current_user.role == "keeper":
        pending = db.query(models.SupplyRequest).filter(
            models.SupplyRequest.status == "pending"
        ).all()
        stats["pending_approval"] = len(pending)
        approved_waiting = db.query(models.SupplyRequest).filter(
            models.SupplyRequest.status.in_(["approved", "partially_approved"])
        ).all()
        stats["waiting_issue"] = len(approved_waiting)
        supplies_count = db.query(models.Supply).count()
        stats["supplies_count"] = supplies_count
        batches_count = db.query(models.SupplyBatch).count()
        stats["batches_count"] = batches_count
    elif current_user.role == "director":
        dept = current_user.department
        budget = db.query(models.BudgetRecord).filter(
            models.BudgetRecord.department == dept,
            models.BudgetRecord.fiscal_year == date.today().year
        ).first()
        used = inv.calculate_budget_usage(db, dept, date.today().year)
        stats["budget_total"] = budget.total_budget if budget else 0
        stats["budget_used"] = used
        stats["budget_ratio"] = round(used / budget.total_budget * 100, 1) if budget and budget.total_budget > 0 else 0
        over_budget = stats["budget_ratio"] > 80
        stats["over_budget"] = over_budget
        dept_users = db.query(models.User).filter(models.User.department == dept).all()
        dept_user_ids = [u.id for u in dept_users]
        dept_requests = db.query(models.SupplyRequest).filter(
            models.SupplyRequest.applicant_id.in_(dept_user_ids)
        ).all()
        stats["dept_requests"] = len(dept_requests)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "current_user": current_user,
        "stats": stats,
        "STATUS_MAP": STATUS_MAP,
        "ROLE_MAP": ROLE_MAP,
        "today": date.today().isoformat()
    })


@app.get("/supplies", response_class=HTMLResponse)
async def supply_list(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    q: str = "",
    category: str = "",
    db: Session = Depends(get_db)
):
    query = db.query(models.Supply)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(models.Supply.name.like(like), models.Supply.specification.like(like)))
    if category:
        query = query.filter(models.Supply.category == category)
    supplies = query.order_by(models.Supply.id).all()

    supply_data = []
    for s in supplies:
        total_stock = inv.get_supply_total_stock(db, s.id)
        available = inv.get_supply_total_available(db, s.id)
        total_frozen = sum(b.frozen_quantity for b in s.batches)
        expired_count = sum(1 for b in s.batches if inv.is_batch_expired(b))
        supply_data.append({
            "supply": s,
            "total_stock": total_stock,
            "available": available,
            "total_frozen": total_frozen,
            "expired_count": expired_count
        })

    return templates.TemplateResponse("supplies/list.html", {
        "request": request,
        "current_user": current_user,
        "supply_data": supply_data,
        "q": q,
        "category": category,
        "CATEGORY_MAP": CATEGORY_MAP,
        "RISK_MAP": RISK_MAP,
        "ROLE_MAP": ROLE_MAP
    })


@app.get("/supplies/new", response_class=HTMLResponse)
async def supply_new_form(
    request: Request,
    current_user: models.User = Depends(get_current_user)
):
    if current_user.role not in ("keeper", "director"):
        raise HTTPException(status_code=403, detail="无权限")
    return templates.TemplateResponse("supplies/form.html", {
        "request": request,
        "current_user": current_user,
        "supply": None,
        "CATEGORY_MAP": CATEGORY_MAP,
        "RISK_MAP": RISK_MAP,
        "ROLE_MAP": ROLE_MAP
    })


@app.post("/supplies/new")
async def supply_create(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    name: str = Form(...),
    category: str = Form(...),
    unit: str = Form(...),
    unit_price: float = Form(0.0),
    is_high_risk: bool = Form(False),
    risk_level: str = Form("low"),
    specification: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db)
):
    if current_user.role not in ("keeper", "director"):
        raise HTTPException(status_code=403, detail="无权限")
    supply = models.Supply(
        name=name, category=category, unit=unit,
        unit_price=unit_price, is_high_risk=is_high_risk,
        risk_level=risk_level, specification=specification,
        description=description
    )
    db.add(supply)
    db.commit()
    return RedirectResponse(f"/supplies/{supply.id}", status_code=303)


@app.get("/supplies/{supply_id}", response_class=HTMLResponse)
async def supply_detail(
    request: Request,
    supply_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    supply = db.query(models.Supply).filter(models.Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(status_code=404, detail="耗材不存在")

    total_stock = inv.get_supply_total_stock(db, supply_id)
    available = inv.get_supply_total_available(db, supply_id)
    total_frozen = sum(b.frozen_quantity for b in supply.batches)
    recent_txns = inv.get_recent_transactions(db, supply_id, limit=20)

    return templates.TemplateResponse("supplies/detail.html", {
        "request": request,
        "current_user": current_user,
        "supply": supply,
        "total_stock": total_stock,
        "available": available,
        "total_frozen": total_frozen,
        "recent_txns": recent_txns,
        "CATEGORY_MAP": CATEGORY_MAP,
        "RISK_MAP": RISK_MAP,
        "ROLE_MAP": ROLE_MAP,
        "TXN_TYPE_MAP": TXN_TYPE_MAP,
        "is_batch_expired": inv.is_batch_expired,
        "today": date.today()
    })


@app.get("/batches/new", response_class=HTMLResponse)
async def batch_new_form(
    request: Request,
    supply_id: int = 0,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role not in ("keeper",):
        raise HTTPException(status_code=403, detail="无权限")
    supplies = db.query(models.Supply).order_by(models.Supply.name).all()
    return templates.TemplateResponse("batches/form.html", {
        "request": request,
        "current_user": current_user,
        "supplies": supplies,
        "selected_supply_id": supply_id,
        "ROLE_MAP": ROLE_MAP
    })


@app.post("/batches/new")
async def batch_create(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    supply_id: int = Form(...),
    batch_no: str = Form(...),
    production_date: str = Form(...),
    expiry_date: str = Form(...),
    total_quantity: float = Form(...),
    supplier: str = Form(""),
    storage_location: str = Form(""),
    remark: str = Form(""),
    db: Session = Depends(get_db)
):
    if current_user.role not in ("keeper",):
        raise HTTPException(status_code=403, detail="无权限")

    existing = db.query(models.SupplyBatch).filter(
        models.SupplyBatch.batch_no == batch_no
    ).first()
    if existing:
        supplies = db.query(models.Supply).order_by(models.Supply.name).all()
        return templates.TemplateResponse("batches/form.html", {
            "request": request,
            "current_user": current_user,
            "supplies": supplies,
            "selected_supply_id": supply_id,
            "error": "该批次号已存在",
            "ROLE_MAP": ROLE_MAP
        })

    batch = models.SupplyBatch(
        supply_id=supply_id,
        batch_no=batch_no,
        production_date=date.fromisoformat(production_date),
        expiry_date=date.fromisoformat(expiry_date),
        total_quantity=total_quantity,
        available_quantity=total_quantity,
        frozen_quantity=0,
        supplier=supplier,
        storage_location=storage_location,
        remark=remark
    )
    db.add(batch)
    db.flush()

    inv.create_transaction(
        db, batch.id, "in", total_quantity,
        operator_id=current_user.id,
        reference_type="manual",
        remark=f"入库：{batch.batch_no}"
    )
    db.commit()
    return RedirectResponse(f"/supplies/{supply_id}", status_code=303)


@app.get("/plans", response_class=HTMLResponse)
async def plan_list(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    query = db.query(models.ExperimentPlan)
    if current_user.role == "teacher":
        query = query.filter(models.ExperimentPlan.creator_id == current_user.id)
    plans = query.order_by(models.ExperimentPlan.plan_date.desc()).all()
    return templates.TemplateResponse("plans/list.html", {
        "request": request,
        "current_user": current_user,
        "plans": plans,
        "ROLE_MAP": ROLE_MAP
    })


@app.get("/plans/new", response_class=HTMLResponse)
async def plan_new_form(
    request: Request,
    current_user: models.User = Depends(get_current_user)
):
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="仅教师可创建实验计划")
    return templates.TemplateResponse("plans/form.html", {
        "request": request,
        "current_user": current_user,
        "plan": None,
        "ROLE_MAP": ROLE_MAP,
        "today": date.today().isoformat()
    })


@app.post("/plans/new")
async def plan_create(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    title: str = Form(...),
    course_name: str = Form(""),
    class_name: str = Form(""),
    student_count: int = Form(0),
    plan_date: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db)
):
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="仅教师可创建实验计划")
    plan = models.ExperimentPlan(
        title=title, course_name=course_name, class_name=class_name,
        student_count=student_count,
        plan_date=date.fromisoformat(plan_date),
        description=description,
        creator_id=current_user.id
    )
    db.add(plan)
    db.commit()
    return RedirectResponse("/plans", status_code=303)


@app.get("/plans/{plan_id}", response_class=HTMLResponse)
async def plan_detail(
    request: Request,
    plan_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    plan = db.query(models.ExperimentPlan).filter(models.ExperimentPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="实验计划不存在")
    return templates.TemplateResponse("plans/detail.html", {
        "request": request,
        "current_user": current_user,
        "plan": plan,
        "ROLE_MAP": ROLE_MAP
    })


@app.get("/requests", response_class=HTMLResponse)
async def request_list(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    status: str = "",
    db: Session = Depends(get_db)
):
    query = db.query(models.SupplyRequest)
    if current_user.role == "teacher":
        query = query.filter(models.SupplyRequest.applicant_id == current_user.id)
    if status:
        query = query.filter(models.SupplyRequest.status == status)
    requests = query.order_by(models.SupplyRequest.created_at.desc()).all()

    can_approve_list = {}
    for r in requests:
        can_approve_list[r.id] = inv.can_approve(current_user, r)

    return templates.TemplateResponse("requests/list.html", {
        "request": request,
        "current_user": current_user,
        "requests": requests,
        "STATUS_MAP": STATUS_MAP,
        "URGENCY_MAP": URGENCY_MAP,
        "ROLE_MAP": ROLE_MAP,
        "can_approve_list": can_approve_list,
        "filter_status": status
    })


@app.get("/requests/new", response_class=HTMLResponse)
async def request_new_form(
    request: Request,
    plan_id: int = 0,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="仅教师可创建领用申请")
    plans = db.query(models.ExperimentPlan).filter(
        models.ExperimentPlan.creator_id == current_user.id
    ).order_by(models.ExperimentPlan.plan_date.desc()).all()
    supplies = db.query(models.Supply).order_by(models.Supply.name).all()

    supply_avail = {}
    for s in supplies:
        supply_avail[s.id] = inv.get_supply_total_available(db, s.id)

    return templates.TemplateResponse("requests/form.html", {
        "request": request,
        "current_user": current_user,
        "plans": plans,
        "selected_plan_id": plan_id,
        "supplies": supplies,
        "supply_avail": supply_avail,
        "ROLE_MAP": ROLE_MAP,
        "URGENCY_MAP": URGENCY_MAP,
        "CATEGORY_MAP": CATEGORY_MAP
    })


@app.post("/requests/new")
async def request_create(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    plan_id: int = Form(...),
    urgency: str = Form("normal"),
    remark: str = Form(""),
    db: Session = Depends(get_db)
):
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="仅教师可创建领用申请")

    form_data = await request.form()
    supply_ids = []
    quantities = []
    remarks = []
    for key, value in form_data.multi_items():
        if key.startswith("supply_id_"):
            idx = key.split("_")[-1]
            qty_key = f"quantity_{idx}"
            rmk_key = f"item_remark_{idx}"
            if qty_key in form_data:
                try:
                    qty = float(form_data[qty_key])
                    if qty > 0:
                        supply_ids.append(int(value))
                        quantities.append(qty)
                        remarks.append(form_data.get(rmk_key, ""))
                except (ValueError, TypeError):
                    pass

    if not supply_ids:
        plans = db.query(models.ExperimentPlan).filter(
            models.ExperimentPlan.creator_id == current_user.id
        ).order_by(models.ExperimentPlan.plan_date.desc()).all()
        supplies = db.query(models.Supply).order_by(models.Supply.name).all()
        supply_avail = {}
        for s in supplies:
            supply_avail[s.id] = inv.get_supply_total_available(db, s.id)
        return templates.TemplateResponse("requests/form.html", {
            "request": request,
            "current_user": current_user,
            "plans": plans,
            "selected_plan_id": plan_id,
            "supplies": supplies,
            "supply_avail": supply_avail,
            "ROLE_MAP": ROLE_MAP,
            "URGENCY_MAP": URGENCY_MAP,
            "CATEGORY_MAP": CATEGORY_MAP,
            "error": "请至少添加一项耗材申请"
        })

    for i, sid in enumerate(supply_ids):
        avail = inv.get_supply_total_available(db, sid)
        if avail < quantities[i]:
            supply = db.query(models.Supply).filter(models.Supply.id == sid).first()
            plans = db.query(models.ExperimentPlan).filter(
                models.ExperimentPlan.creator_id == current_user.id
            ).order_by(models.ExperimentPlan.plan_date.desc()).all()
            supplies = db.query(models.Supply).order_by(models.Supply.name).all()
            supply_avail = {}
            for s in supplies:
                supply_avail[s.id] = inv.get_supply_total_available(db, s.id)
            return templates.TemplateResponse("requests/form.html", {
                "request": request,
                "current_user": current_user,
                "plans": plans,
                "selected_plan_id": plan_id,
                "supplies": supplies,
                "supply_avail": supply_avail,
                "ROLE_MAP": ROLE_MAP,
                "URGENCY_MAP": URGENCY_MAP,
                "CATEGORY_MAP": CATEGORY_MAP,
                "error": f"耗材【{supply.name}】可用库存不足（可用 {avail} {supply.unit}），申请数量 {quantities[i]} {supply.unit}"
            })

    req = models.SupplyRequest(
        plan_id=plan_id,
        applicant_id=current_user.id,
        status="pending",
        urgency=urgency,
        remark=remark
    )
    db.add(req)
    db.flush()

    total = 0.0
    for i, sid in enumerate(supply_ids):
        supply = db.query(models.Supply).filter(models.Supply.id == sid).first()
        item = models.RequestItem(
            request_id=req.id,
            supply_id=sid,
            requested_quantity=quantities[i],
            remark=remarks[i]
        )
        db.add(item)
        total += supply.unit_price * quantities[i]

    req.total_amount = total
    db.commit()
    return RedirectResponse(f"/requests/{req.id}", status_code=303)


@app.get("/requests/{request_id}", response_class=HTMLResponse)
async def request_detail(
    request: Request,
    request_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    req = db.query(models.SupplyRequest).filter(models.SupplyRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")

    can_approve = inv.can_approve(current_user, req)
    can_issue = current_user.role == "keeper" and req.status in ("approved", "partially_approved")

    item_issuances = {}
    for item in req.items:
        item_issuances[item.id] = db.query(models.SupplyIssuance).filter(
            models.SupplyIssuance.item_id == item.id
        ).all()

    item_batches = {}
    for item in req.items:
        valid_batches = []
        for b in item.supply.batches:
            if not inv.is_batch_expired(b) and b.available_quantity > 0:
                valid_batches.append(b)
        valid_batches.sort(key=lambda x: x.expiry_date)
        item_batches[item.id] = valid_batches

    return templates.TemplateResponse("requests/detail.html", {
        "request": request,
        "current_user": current_user,
        "req": req,
        "can_approve": can_approve,
        "can_issue": can_issue,
        "STATUS_MAP": STATUS_MAP,
        "URGENCY_MAP": URGENCY_MAP,
        "ROLE_MAP": ROLE_MAP,
        "item_issuances": item_issuances,
        "item_batches": item_batches,
        "is_batch_expired": inv.is_batch_expired,
        "today": date.today()
    })


@app.post("/requests/{request_id}/approve")
async def request_approve(
    request: Request,
    request_id: int,
    current_user: models.User = Depends(get_current_user),
    decision: str = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_db)
):
    req = db.query(models.SupplyRequest).filter(models.SupplyRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if not inv.can_approve(current_user, req):
        return templates.TemplateResponse("error.html", {
            "request": request,
            "current_user": current_user,
            "error": "无权限审批此申请，或不能审批自己的申请",
            "ROLE_MAP": ROLE_MAP
        })
    if req.status not in ("pending", "partially_approved"):
        return templates.TemplateResponse("error.html", {
            "request": request,
            "current_user": current_user,
            "error": "此申请状态不允许审批",
            "ROLE_MAP": ROLE_MAP
        })

    form_data = await request.form()

    approval = models.ApprovalRecord(
        request_id=req.id,
        approver_id=current_user.id,
        step=len(req.approvals) + 1,
        decision=decision,
        comment=comment
    )
    db.add(approval)

    if decision == "approve":
        item_quantities = {}
        for item in req.items:
            key = f"approved_qty_{item.id}"
            if key in form_data and form_data[key]:
                try:
                    qty = float(form_data[key])
                    qty = min(qty, item.requested_quantity)
                except ValueError:
                    qty = item.requested_quantity
            else:
                qty = item.requested_quantity

            avail = inv.get_supply_total_available(db, item.supply_id)
            qty = min(qty, avail)
            item.approved_quantity = qty
            item.unit_price_at_approval = item.supply.unit_price
            item_quantities[item.id] = qty

        try:
            inv.freeze_request_items(db, req, item_quantities, current_user.id)
        except ValueError as e:
            db.rollback()
            return templates.TemplateResponse("error.html", {
                "request": request,
                "current_user": current_user,
                "error": f"冻结库存失败：{e}",
                "ROLE_MAP": ROLE_MAP
            })

        all_full = all(
            item.approved_quantity >= item.requested_quantity
            for item in req.items
        )
        req.status = "approved" if all_full else "partially_approved"

    elif decision == "reject":
        inv.unfreeze_request_items(db, req, current_user.id)
        req.status = "rejected"

    elif decision == "partial":
        item_quantities = {}
        for item in req.items:
            key = f"approved_qty_{item.id}"
            if key in form_data and form_data[key]:
                try:
                    qty = float(form_data[key])
                except ValueError:
                    qty = 0
            else:
                qty = 0
            qty = max(0, min(qty, item.requested_quantity))
            avail = inv.get_supply_total_available(db, item.supply_id)
            qty = min(qty, avail)
            item.approved_quantity = qty
            item.unit_price_at_approval = item.supply.unit_price
            item_quantities[item.id] = qty

        total_approved = sum(item.approved_quantity for item in req.items)
        if total_approved <= 0:
            db.rollback()
            return templates.TemplateResponse("error.html", {
                "request": request,
                "current_user": current_user,
                "error": "部分批准时至少需要批准一项耗材",
                "ROLE_MAP": ROLE_MAP
            })

        try:
            inv.freeze_request_items(db, req, item_quantities, current_user.id)
        except ValueError as e:
            db.rollback()
            return templates.TemplateResponse("error.html", {
                "request": request,
                "current_user": current_user,
                "error": f"冻结库存失败：{e}",
                "ROLE_MAP": ROLE_MAP
            })

        req.status = "partially_approved"

    db.commit()
    return RedirectResponse(f"/requests/{req.id}", status_code=303)


@app.post("/requests/{request_id}/issue")
async def request_issue(
    request: Request,
    request_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    req = db.query(models.SupplyRequest).filter(models.SupplyRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if current_user.role != "keeper":
        raise HTTPException(status_code=403, detail="仅库管员可发放耗材")
    if req.status not in ("approved", "partially_approved"):
        return templates.TemplateResponse("error.html", {
            "request": request,
            "current_user": current_user,
            "error": "此申请状态不允许发放",
            "ROLE_MAP": ROLE_MAP
        })

    form_data = await request.form()
    receiver_name = form_data.get("receiver_name", req.applicant.full_name)

    issued_any = False
    for item in req.items:
        remaining = item.approved_quantity - item.issued_quantity
        if remaining <= 0:
            continue

        batch_key = f"batch_id_{item.id}"
        qty_key = f"issue_qty_{item.id}"
        if batch_key not in form_data or qty_key not in form_data:
            continue

        try:
            batch_id = int(form_data[batch_key])
            issue_qty = float(form_data[qty_key])
        except (ValueError, TypeError):
            continue

        if issue_qty <= 0 or batch_id <= 0:
            continue

        batch = db.query(models.SupplyBatch).filter(models.SupplyBatch.id == batch_id).first()
        if not batch:
            continue

        if inv.is_batch_expired(batch):
            return templates.TemplateResponse("error.html", {
                "request": request,
                "current_user": current_user,
                "error": f"批次【{batch.batch_no}】已过期，禁止发放",
                "ROLE_MAP": ROLE_MAP
            })

        if batch.frozen_quantity < issue_qty:
            return templates.TemplateResponse("error.html", {
                "request": request,
                "current_user": current_user,
                "error": f"批次【{batch.batch_no}】冻结数量不足，无法发放 {issue_qty}",
                "ROLE_MAP": ROLE_MAP
            })

        confirmed_key = f"confirm_{item.id}"
        already_confirmed = db.query(models.SupplyIssuance).filter(
            models.SupplyIssuance.item_id == item.id,
            models.SupplyIssuance.confirmed == True
        ).all()
        already_confirmed_total = sum(i.quantity for i in already_confirmed)
        if already_confirmed_total + issue_qty > item.approved_quantity:
            return templates.TemplateResponse("error.html", {
                "request": request,
                "current_user": current_user,
                "error": f"确认发放数量超过批准数量，无法重复发放",
                "ROLE_MAP": ROLE_MAP
            })

        try:
            inv.issue_from_frozen(
                db, req, item, batch_id, issue_qty,
                current_user.id, receiver_name
            )
            issued_any = True

            if confirmed_key in form_data:
                last_issuance = db.query(models.SupplyIssuance).filter(
                    models.SupplyIssuance.item_id == item.id
                ).order_by(models.SupplyIssuance.id.desc()).first()
                if last_issuance and not last_issuance.confirmed:
                    last_issuance.confirmed = True
                    last_issuance.confirmed_at = datetime.now()

        except ValueError as e:
            db.rollback()
            return templates.TemplateResponse("error.html", {
                "request": request,
                "current_user": current_user,
                "error": f"发放失败：{e}",
                "ROLE_MAP": ROLE_MAP
            })

    if not issued_any:
        return templates.TemplateResponse("error.html", {
            "request": request,
            "current_user": current_user,
            "error": "未选择有效的发放项",
            "ROLE_MAP": ROLE_MAP
        })

    all_issued = all(
        item.issued_quantity >= item.approved_quantity
        for item in req.items
    )
    if all_issued:
        req.status = "completed"

    db.commit()
    return RedirectResponse(f"/requests/{req.id}", status_code=303)


@app.post("/requests/{request_id}/reject-release")
async def request_reject_release(
    request: Request,
    request_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    req = db.query(models.SupplyRequest).filter(models.SupplyRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="申请不存在")
    if current_user.role not in ("keeper", "director"):
        raise HTTPException(status_code=403, detail="无权限")
    if req.status in ("rejected", "completed"):
        return RedirectResponse(f"/requests/{req.id}", status_code=303)

    inv.unfreeze_request_items(db, req, current_user.id)
    req.status = "rejected"

    approval = models.ApprovalRecord(
        request_id=req.id,
        approver_id=current_user.id,
        step=len(req.approvals) + 1,
        decision="reject",
        comment="驳回并释放冻结库存"
    )
    db.add(approval)
    db.commit()
    return RedirectResponse(f"/requests/{req.id}", status_code=303)


@app.get("/director/overview", response_class=HTMLResponse)
async def director_overview(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role != "director":
        raise HTTPException(status_code=403, detail="仅系主任可查看此页面")

    dept = current_user.department
    today = date.today()
    year = today.year

    budget = db.query(models.BudgetRecord).filter(
        models.BudgetRecord.department == dept,
        models.BudgetRecord.fiscal_year == year
    ).first()
    used = inv.calculate_budget_usage(db, dept, year)
    budget_total = budget.total_budget if budget else 0
    budget_ratio = round(used / budget_total * 100, 1) if budget_total > 0 else 0
    over_budget = budget_ratio > 80

    dept_users = db.query(models.User).filter(models.User.department == dept).all()
    dept_user_ids = [u.id for u in dept_users]

    budget_by_month = {}
    for m in range(1, 13):
        budget_by_month[m] = 0.0

    month_label = lambda m: f"{year}-{m:02d}"

    dept_issuances = db.query(models.SupplyIssuance).join(
        models.SupplyRequest, models.SupplyIssuance.request_id == models.SupplyRequest.id
    ).filter(
        models.SupplyRequest.applicant_id.in_(dept_user_ids),
        models.SupplyIssuance.confirmed == True
    ).all()

    for iss in dept_issuances:
        if iss.issued_at and iss.issued_at.year == year:
            item = db.query(models.RequestItem).filter(models.RequestItem.id == iss.item_id).first()
            if item:
                price = item.unit_price_at_approval
                if price == 0 and item.supply:
                    price = item.supply.unit_price
                m = iss.issued_at.month
                budget_by_month[m] += price * iss.quantity

    high_risk_supplies = db.query(models.Supply).filter(models.Supply.is_high_risk == True).all()
    high_risk_usage = []
    for hs in high_risk_supplies:
        supply_txns = []
        for b in hs.batches:
            for t in b.transactions:
                if t.transaction_type == "out" and t.reference_type == "issuance":
                    iss = db.query(models.SupplyIssuance).filter(
                        models.SupplyIssuance.id == t.reference_id
                    ).first()
                    if iss and iss.request and iss.request.applicant_id in dept_user_ids:
                        supply_txns.append(t)
        total_qty = sum(t.quantity for t in supply_txns)
        total_amt = sum(
            t.quantity * (hs.unit_price)
            for t in supply_txns
        )
        high_risk_usage.append({
            "supply": hs,
            "total_qty": total_qty,
            "total_amt": total_amt,
            "txn_count": len(supply_txns)
        })

    over_budget_requests = []
    for uid in dept_user_ids:
        reqs = db.query(models.SupplyRequest).filter(
            models.SupplyRequest.applicant_id == uid,
            models.SupplyRequest.total_amount >= 1000
        ).order_by(models.SupplyRequest.created_at.desc()).all()
        over_budget_requests.extend(reqs)

    return templates.TemplateResponse("director/overview.html", {
        "request": request,
        "current_user": current_user,
        "ROLE_MAP": ROLE_MAP,
        "STATUS_MAP": STATUS_MAP,
        "RISK_MAP": RISK_MAP,
        "dept": dept,
        "budget_total": budget_total,
        "budget_used": used,
        "budget_remaining": budget_total - used,
        "budget_ratio": budget_ratio,
        "over_budget": over_budget,
        "budget_by_month": budget_by_month,
        "high_risk_usage": high_risk_usage,
        "over_budget_requests": over_budget_requests,
        "month_label": month_label,
        "today": today
    })


@app.get("/export/csv")
async def export_csv(
    request: Request,
    month: str = "",
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role not in ("keeper", "director"):
        raise HTTPException(status_code=403, detail="无权限导出")

    if not month:
        month = date.today().strftime("%Y-%m")

    try:
        year = int(month.split("-")[0])
        m = int(month.split("-")[1])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="月份格式错误，应为 YYYY-MM")

    start_date = date(year, m, 1)
    if m == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, m + 1, 1)

    issuances = db.query(models.SupplyIssuance).filter(
        models.SupplyIssuance.issued_at >= datetime.combine(start_date, datetime.min.time()),
        models.SupplyIssuance.issued_at < datetime.combine(end_date, datetime.min.time())
    ).order_by(models.SupplyIssuance.issued_at).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "发放单号", "领用申请号", "领用日期", "申请人", "申请人部门",
        "耗材名称", "耗材类别", "规格", "批次号", "生产批号",
        "有效期", "发放数量", "单位", "单价", "金额",
        "库管员", "领用人", "确认状态"
    ])

    total_amount = 0.0
    for iss in issuances:
        item = db.query(models.RequestItem).filter(models.RequestItem.id == iss.item_id).first()
        if not item:
            continue
        supply = item.supply
        batch = iss.batch
        price = item.unit_price_at_approval if item.unit_price_at_approval else supply.unit_price
        amount = price * iss.quantity
        total_amount += amount
        applicant = iss.request.applicant if iss.request else None
        keeper = iss.keeper

        writer.writerow([
            iss.id,
            iss.request_id,
            iss.issued_at.strftime("%Y-%m-%d %H:%M") if iss.issued_at else "",
            applicant.full_name if applicant else "",
            applicant.department if applicant else "",
            supply.name,
            CATEGORY_MAP.get(supply.category, supply.category),
            supply.specification,
            batch.batch_no if batch else "",
            batch.batch_no if batch else "",
            batch.expiry_date.strftime("%Y-%m-%d") if batch and batch.expiry_date else "",
            iss.quantity,
            supply.unit,
            f"{price:.2f}",
            f"{amount:.2f}",
            keeper.full_name if keeper else "",
            iss.receiver_name,
            "已确认" if iss.confirmed else "待确认"
        ])

    writer.writerow([])
    writer.writerow(["合计金额", "", "", "", "", "", "", "", "", "", "", "", "", "", f"{total_amount:.2f}", "", "", ""])

    csv_content = output.getvalue()
    filename = f"领用明细_{month}.csv"
    from urllib.parse import quote
    encoded_filename = quote(filename)

    response = Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8-sig"
    )
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}; filename=\"{month}.csv\""
    return response


@app.get("/export", response_class=HTMLResponse)
async def export_page(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role not in ("keeper", "director"):
        raise HTTPException(status_code=403, detail="无权限")

    today = date.today()
    months = []
    for i in range(12):
        d = today.replace(day=1)
        if d.month - i <= 0:
            m = d.month - i + 12
            y = d.year - 1
        else:
            m = d.month - i
            y = d.year
        months.append(f"{y}-{m:02d}")

    return templates.TemplateResponse("export.html", {
        "request": request,
        "current_user": current_user,
        "ROLE_MAP": ROLE_MAP,
        "months": months,
        "today": today
    })


@app.get("/error", response_class=HTMLResponse)
async def error_page(
    request: Request,
    msg: str = "",
    current_user: models.User = Depends(get_current_user)
):
    return templates.TemplateResponse("error.html", {
        "request": request,
        "current_user": current_user,
        "error": msg,
        "ROLE_MAP": ROLE_MAP
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
