"""冻结时机真实验证脚本"""
import sys
import requests
from datetime import date, timedelta

BASE = "http://127.0.0.1:8000"

from database import SessionLocal, engine
from models import Base, User, Supply, SupplyBatch, ExperimentPlan, SupplyRequest, RequestItem, ApprovalRecord, SupplyIssuance, InventoryTransaction
import inventory as inv


def login(s, username, pwd="123456"):
    r = s.post(f"{BASE}/login", data={"username": username, "password": pwd}, allow_redirects=True)
    return "欢迎回来" in r.text


def logout(s):
    s.post(f"{BASE}/logout", allow_redirects=True)
    s.cookies.clear()


def create_plan(s, title="验证计划"):
    today = date.today().isoformat()
    r = s.post(f"{BASE}/plans/new", data={
        "title": title,
        "course_name": "验证课程",
        "class_name": "验2401",
        "student_count": 30,
        "plan_date": today,
        "description": "验证用"
    }, allow_redirects=True)
    import re
    ids = [int(x) for x in re.findall(r'href="/plans/(\d+)"', r.text)]
    return ids[0] if ids else None


def submit_request(s, plan_id, supplies_and_qty):
    """supplies_and_qty: [(supply_id, qty, remark), ...] 允许重复supply_id（多行）"""
    data = {
        "plan_id": str(plan_id),
        "urgency": "normal",
        "remark": "验证",
    }
    for i, (sid, qty, rmk) in enumerate(supplies_and_qty):
        data[f"supply_id_{i}"] = str(sid)
        data[f"quantity_{i}"] = str(qty)
        data[f"item_remark_{i}"] = rmk
    r = s.post(f"{BASE}/requests/new", data=data, allow_redirects=True)
    import re
    rid = None
    m = re.search(r'/requests/(\d+)', r.url)
    if m:
        rid = int(m.group(1))
    else:
        ids = [int(x) for x in re.findall(r'href="/requests/(\d+)"', r.text)]
        rid = max(ids) if ids else None
    in_form = rid is None and ("创建领用申请" in r.text or "请至少添加" in r.text or "可用库存不足" in r.text)
    error = None
    if "可用库存不足" in r.text:
        m2 = re.search(r'error[^>]*>([^<]+)', r.text)
        if m2:
            error = m2.group(1).strip()
    return rid, in_form, error, r


def get_latest_request_id(s):
    import re
    r = s.get(f"{BASE}/requests", allow_redirects=True)
    ids = [int(x) for x in re.findall(r'href="/requests/(\d+)"', r.text)]
    return max(ids) if ids else None


def main():
    db = SessionLocal()
    s1 = requests.Session()
    s2 = requests.Session()
    passed = 0
    failed = 0

    def check(cond, msg):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"[PASS] {msg}")
        else:
            failed += 1
            print(f"[FAIL] {msg}")

    print("=" * 60)
    print("场景1: 同一批次只剩2件时，两张各申请2件的待审单只能成功一张")
    print("=" * 60)

    # 准备：把丁腈手套的其他批次冻结量清零（过期或不可用），新建测试批次2件
    glove = db.query(Supply).filter(Supply.name == "丁腈手套").first()
    # 将丁腈手套其他批次数量置0（通过流水调整）
    for b in glove.batches:
        if b.available_quantity > 0 or b.frozen_quantity > 0:
            # 把有效量设为0（adjust）
            inv.create_transaction(db, b.id, "adjust", 0, operator_id=None,
                                   reference_type="system", remark="测试前归零")
            b.available_quantity = 0
            b.frozen_quantity = 0

    today = date.today()
    expiry = today + timedelta(days=365)
    test_batch = SupplyBatch(
        supply_id=glove.id,
        batch_no="TEST-FREEZE-2PCS",
        production_date=today,
        expiry_date=expiry,
        total_quantity=0,
        available_quantity=0,
        frozen_quantity=0,
        supplier="测试",
        storage_location="测试架",
        remark="用于验证库存冻结竞争"
    )
    db.add(test_batch)
    db.flush()
    inv.create_transaction(db, test_batch.id, "in", 2, operator_id=None,
                           reference_type="system", remark="测试入库")
    db.commit()
    db.refresh(test_batch)
    print(f"  -> 创建批次 TEST-FREEZE-2PCS，可用={test_batch.available_quantity}，冻结={test_batch.frozen_quantity}")

    check(test_batch.available_quantity == 2 and test_batch.frozen_quantity == 0,
          "测试批次初始：可用=2 冻结=0")

    # teacher1 提交申请1：2件丁腈手套
    assert login(s1, "teacher1")
    plan_id1 = create_plan(s1, "张老师 2件申请")
    rid1, in_form, err, resp = submit_request(s1, plan_id1, [(glove.id, 2, "申请2件-1")])
    check(rid1 is not None and not in_form, f"teacher1 2件申请提交成功，申请ID={rid1}")

    db.refresh(test_batch)
    print(f"  -> 申请1提交后，批次：可用={test_batch.available_quantity}，冻结={test_batch.frozen_quantity}")
    check(test_batch.frozen_quantity == 2,
          "提交申请1后立即冻结：批次冻结数量=2")

    # teacher2 提交申请2：2件丁腈手套（应失败，因已被冻结）
    s2.cookies.clear()
    assert login(s2, "teacher2")
    plan_id2 = create_plan(s2, "李老师 2件申请")
    rid2, in_form, err, resp2 = submit_request(s2, plan_id2, [(glove.id, 2, "申请2件-2")])
    check(rid2 is None and in_form, "teacher2 再申请2件失败（仍在申请表单页）")
    check(err is not None and "可用库存不足" in err, f"失败原因是库存不足：{err}")

    # 查冻结流水
    txns = db.query(InventoryTransaction).filter(
        InventoryTransaction.batch_id == test_batch.id
    ).order_by(InventoryTransaction.id).all()
    print(f"  -> 测试批次流水：")
    for t in txns:
        print(f"     id={t.id} type={t.transaction_type} qty={t.quantity} bal_before={t.balance_before} bal_after={t.balance_after} fro_before={t.frozen_before} fro_after={t.frozen_after} ref={t.reference_type}/{t.reference_id}")

    freeze_txns = [t for t in txns if t.transaction_type == "freeze"]
    check(len(freeze_txns) == 1, "丁腈手套测试批次只有1条冻结流水（申请1的）")
    check(freeze_txns[0].reference_type == "request" and freeze_txns[0].reference_id == rid1,
          "冻结流水关联到申请1")

    print()
    print("=" * 60)
    print("场景2: 拆成多行超过库存必须失败（同一耗材多行汇总）")
    print("=" * 60)

    # 氯化钠：假设库存是某值。我们用丁腈手套测试批次，只剩0可用（已冻结2）
    rid3, in_form, err, resp3 = submit_request(
        s2, plan_id2,
        [
            (glove.id, 1, "第1行1件"),
            (glove.id, 1, "第2行1件"),
        ])
    check(rid3 is None and in_form, "同一耗材拆成2行各1件 提交失败（总2件>可用0）")
    check(err is not None and "多行已汇总" in err, f"错误提示说明已汇总：{err}")

    # 再试：可用的氯化钠
    nacl = db.query(Supply).filter(Supply.name == "氯化钠").first()
    nacl_avail = inv.get_supply_total_available(db, nacl.id)
    print(f"  -> 氯化钠当前可用={nacl_avail}")
    over_qty = nacl_avail + 5
    half = over_qty / 2
    rid4, in_form, err, resp4 = submit_request(
        s1, plan_id1,
        [
            (nacl.id, half, f"超过库存第1行{half}"),
            (nacl.id, half, f"超过库存第2行{half}"),
        ])
    check(rid4 is None and in_form, f"氯化钠拆成2行 各{half} 总量超库存{nacl_avail} 提交失败")
    check(err is not None and "多行已汇总" in err, f"错误提示多行已汇总：{err}")

    print()
    print("=" * 60)
    print("场景3: 驳回后冻结归零，流水可追溯")
    print("=" * 60)

    # 驳回申请1
    logout(s1)
    assert login(s1, "keeper1")  # keeper审批
    # 先记录冻结数
    before_frozen = test_batch.frozen_quantity
    r = s1.post(f"{BASE}/requests/{rid1}/reject-release", data={}, allow_redirects=True)
    db.refresh(test_batch)
    after_frozen = test_batch.frozen_quantity
    check(after_frozen == 0, f"驳回后测试批次冻结归零（冻结：{before_frozen} -> {after_frozen}）")
    req = db.query(SupplyRequest).filter(SupplyRequest.id == rid1).first()
    check(req.status == "rejected", f"申请1状态变为rejected（{req.status}）")

    # 查该批次解冻流水
    txns = db.query(InventoryTransaction).filter(
        InventoryTransaction.batch_id == test_batch.id
    ).order_by(InventoryTransaction.id).all()
    unfreeze_txns = [t for t in txns if t.transaction_type == "unfreeze"]
    check(len(unfreeze_txns) == 1, "驳回后出现1条解冻流水")
    check(unfreeze_txns[0].reference_id == rid1,
          f"解冻流水关联申请{rid1}（实际是 {unfreeze_txns[0].reference_id}）")
    print(f"  -> 解冻流水: qty={unfreeze_txns[0].quantity} ref_id={unfreeze_txns[0].reference_id}")
    print(f"  -> 该批次完整流水（含 in / freeze / unfreeze 类型）：共 {len(txns)} 条，可追溯")

    # 检查库存可用恢复
    db.refresh(test_batch)
    nacl2_avail = inv.get_supply_total_available(db, glove.id)
    # 恢复了2件
    check(test_batch.available_quantity - test_batch.frozen_quantity == 2,
          "驳回后该批次可用恢复为 2")

    # 现在 teacher2 再申请 2 件丁腈手套应该成功
    logout(s1)
    assert login(s2, "teacher2")
    rid5, in_form, err, resp5 = submit_request(s2, plan_id2, [(glove.id, 2, "再次申请")])
    check(rid5 is not None and not in_form, f"驳回解冻后，teacher2 再申请2件成功，申请ID={rid5}")
    db.refresh(test_batch)
    check(test_batch.frozen_quantity == 2, "再次申请成功后批次冻结=2")

    # 场景4（附加）：部分发放后驳回，剩余冻结正确释放
    print()
    print("=" * 60)
    print("场景4(附加): 部分发放后驳回，只释放剩余冻结")
    print("=" * 60)

    logout(s2)
    assert login(s1, "keeper1")
    # 先审批（完全批准）
    r = s1.post(f"{BASE}/requests/{rid5}/approve", data={
        "decision": "approve",
        "comment": "全批"
    }, allow_redirects=True)
    db.refresh(test_batch)
    check(test_batch.frozen_quantity == 2, "审批通过后冻结仍为2（提交时已冻结）")

    # 发放1件
    req5 = db.query(SupplyRequest).filter(SupplyRequest.id == rid5).first()
    item = req5.items[0]
    r = s1.post(f"{BASE}/requests/{rid5}/issue", data={
        f"batch_id_{item.id}": str(test_batch.id),
        f"issue_qty_{item.id}": "1",
        f"confirm_{item.id}": "on",
        "receiver_name": "张老师"
    }, allow_redirects=True)
    db.refresh(test_batch)
    print(f"  -> 发放1件后：批次可用={test_batch.available_quantity}，冻结={test_batch.frozen_quantity}")
    check(test_batch.available_quantity == 1, "发放1件后可用=1")
    check(test_batch.frozen_quantity == 1, "发放1件后剩余冻结=1（2-1=1）")

    # 此时驳回
    r = s1.post(f"{BASE}/requests/{rid5}/reject-release", data={}, allow_redirects=True)
    db.refresh(test_batch)
    print(f"  -> 部分发放后驳回：批次可用={test_batch.available_quantity}，冻结={test_batch.frozen_quantity}")
    check(test_batch.frozen_quantity == 0, "部分发放后驳回：冻结归零（只释放剩余冻结1，已发放的1不再恢复）")
    check(test_batch.available_quantity == 1, "部分发放后驳回：可用仍为1（已发放的不回滚）")

    # 再验证流水
    txns = db.query(InventoryTransaction).filter(
        InventoryTransaction.batch_id == test_batch.id
    ).order_by(InventoryTransaction.id).all()
    print(f"  -> 测试批次完整流水（最终）：")
    for t in txns:
        print(f"     id={t.id} type={t.transaction_type:8s} qty={t.quantity:4.0f} bal={t.balance_before:4.0f}->{t.balance_after:4.0f} fro={t.frozen_before:4.0f}->{t.frozen_after:4.0f} ref={t.reference_type}/{t.reference_id}")

    db.close()
    print()
    print("=" * 60)
    print(f"验证汇总：通过 {passed}，失败 {failed}")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
