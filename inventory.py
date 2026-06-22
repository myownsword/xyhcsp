from datetime import date, datetime
from typing import List, Tuple, Optional

from sqlalchemy.orm import Session
import models


def is_batch_expired(batch: models.SupplyBatch) -> bool:
    return date.today() > batch.expiry_date


def get_supply_total_available(db: Session, supply_id: int) -> float:
    batches = db.query(models.SupplyBatch).filter(
        models.SupplyBatch.supply_id == supply_id
    ).all()
    total = 0.0
    for b in batches:
        if not is_batch_expired(b):
            total += max(0, b.available_quantity - b.frozen_quantity)
    return total


def get_supply_total_stock(db: Session, supply_id: int) -> float:
    batches = db.query(models.SupplyBatch).filter(
        models.SupplyBatch.supply_id == supply_id
    ).all()
    return sum(max(0, b.available_quantity) for b in batches)


def create_transaction(
    db: Session,
    batch_id: int,
    txn_type: str,
    quantity: float,
    operator_id: Optional[int] = None,
    reference_type: str = "",
    reference_id: Optional[int] = None,
    remark: str = ""
) -> models.InventoryTransaction:
    batch = db.query(models.SupplyBatch).filter(models.SupplyBatch.id == batch_id).first()
    if not batch:
        raise ValueError(f"批次不存在: {batch_id}")

    balance_before = batch.available_quantity
    frozen_before = batch.frozen_quantity

    if txn_type == "in":
        batch.available_quantity += quantity
    elif txn_type == "out":
        batch.available_quantity -= quantity
    elif txn_type == "freeze":
        batch.frozen_quantity += quantity
    elif txn_type == "unfreeze":
        batch.frozen_quantity -= quantity
    elif txn_type == "adjust":
        batch.available_quantity = quantity

    txn = models.InventoryTransaction(
        batch_id=batch_id,
        transaction_type=txn_type,
        quantity=quantity,
        balance_before=balance_before,
        balance_after=batch.available_quantity,
        frozen_before=frozen_before,
        frozen_after=batch.frozen_quantity,
        reference_type=reference_type,
        reference_id=reference_id,
        operator_id=operator_id,
        remark=remark
    )
    db.add(txn)
    db.flush()
    return txn


def allocate_batches_for_quantity(
    db: Session,
    supply_id: int,
    quantity: float,
    prefer_earlier_expiry: bool = True
) -> List[Tuple[models.SupplyBatch, float]]:
    batches = db.query(models.SupplyBatch).filter(
        models.SupplyBatch.supply_id == supply_id
    ).all()

    valid_batches = []
    for b in batches:
        if is_batch_expired(b):
            continue
        usable = b.available_quantity - b.frozen_quantity
        if usable > 0:
            valid_batches.append((b, usable))

    if prefer_earlier_expiry:
        valid_batches.sort(key=lambda x: x[0].expiry_date)

    allocation = []
    remaining = quantity
    for batch, usable in valid_batches:
        if remaining <= 0:
            break
        take = min(usable, remaining)
        allocation.append((batch, take))
        remaining -= take

    if remaining > 0:
        raise ValueError(
            f"可用库存不足，需要 {quantity} 但仅能分配 {quantity - remaining}"
        )

    return allocation


def freeze_request_items(
    db: Session,
    request: models.SupplyRequest,
    item_quantities: dict,
    operator_id: int
):
    for item in request.items:
        qty = item_quantities.get(item.id, 0)
        if qty <= 0:
            continue

        allocations = allocate_batches_for_quantity(db, item.supply_id, qty)
        remaining = qty
        for batch, take in allocations:
            if remaining <= 0:
                break
            create_transaction(
                db, batch.id, "freeze", take,
                operator_id=operator_id,
                reference_type="request",
                reference_id=request.id,
                remark=f"申请 #{request.id} 冻结 {item.supply.name}"
            )
            remaining -= take


def unfreeze_request_items(
    db: Session,
    request: models.SupplyRequest,
    operator_id: int
):
    txns = db.query(models.InventoryTransaction).filter(
        models.InventoryTransaction.reference_type == "request",
        models.InventoryTransaction.reference_id == request.id,
        models.InventoryTransaction.transaction_type == "freeze"
    ).all()

    for txn in txns:
        batch = db.query(models.SupplyBatch).filter(
            models.SupplyBatch.id == txn.batch_id
        ).first()
        if batch and batch.frozen_quantity >= txn.quantity:
            create_transaction(
                db, txn.batch_id, "unfreeze", txn.quantity,
                operator_id=operator_id,
                reference_type="request",
                reference_id=request.id,
                remark=f"申请 #{request.id} 解冻 {batch.supply.name}"
            )


def issue_from_frozen(
    db: Session,
    request: models.SupplyRequest,
    item: models.RequestItem,
    batch_id: int,
    quantity: float,
    keeper_id: int,
    receiver_name: str = ""
) -> models.SupplyIssuance:
    batch = db.query(models.SupplyBatch).filter(models.SupplyBatch.id == batch_id).first()
    if not batch:
        raise ValueError("批次不存在")
    if is_batch_expired(batch):
        raise ValueError(f"批次 {batch.batch_no} 已过期，不能发放")
    if batch.frozen_quantity < quantity:
        raise ValueError("冻结数量不足，无法发放")

    batch.frozen_quantity -= quantity
    balance_before = batch.available_quantity
    batch.available_quantity -= quantity

    txn = models.InventoryTransaction(
        batch_id=batch_id,
        transaction_type="out",
        quantity=quantity,
        balance_before=balance_before,
        balance_after=batch.available_quantity,
        frozen_before=batch.frozen_quantity + quantity,
        frozen_after=batch.frozen_quantity,
        reference_type="issuance",
        reference_id=None,
        operator_id=keeper_id,
        remark=f"向 {receiver_name} 发放 {item.supply.name}"
    )
    db.add(txn)
    db.flush()

    issuance = models.SupplyIssuance(
        request_id=request.id,
        item_id=item.id,
        batch_id=batch_id,
        quantity=quantity,
        keeper_id=keeper_id,
        receiver_name=receiver_name or request.applicant.full_name,
        confirmed=False
    )
    db.add(issuance)
    db.flush()
    txn.reference_id = issuance.id

    item.issued_quantity += quantity
    return issuance


def calculate_budget_usage(db: Session, department: str, fiscal_year: int) -> float:
    issuances = db.query(models.SupplyIssuance).join(
        models.SupplyRequest, models.SupplyIssuance.request_id == models.SupplyRequest.id
    ).join(
        models.User, models.SupplyRequest.applicant_id == models.User.id
    ).filter(
        models.User.department == department,
        models.SupplyIssuance.confirmed == True
    ).all()

    total = 0.0
    for iss in issuances:
        if iss.issued_at and iss.issued_at.year == fiscal_year:
            item = db.query(models.RequestItem).filter(
                models.RequestItem.id == iss.item_id
            ).first()
            if item:
                price = item.unit_price_at_approval
                if price == 0 and item.supply:
                    price = item.supply.unit_price
                total += price * iss.quantity
    return total


def calculate_request_total_amount(request: models.SupplyRequest) -> float:
    total = 0.0
    for item in request.items:
        price = item.unit_price_at_approval
        if price == 0 and item.supply:
            price = item.supply.unit_price
        total += price * item.requested_quantity
    return total


def get_recent_transactions(db: Session, supply_id: int, limit: int = 10) -> List[models.InventoryTransaction]:
    batch_ids = [b.id for b in db.query(models.SupplyBatch).filter(
        models.SupplyBatch.supply_id == supply_id
    ).all()]
    if not batch_ids:
        return []
    return db.query(models.InventoryTransaction).filter(
        models.InventoryTransaction.batch_id.in_(batch_ids)
    ).order_by(
        models.InventoryTransaction.created_at.desc()
    ).limit(limit).all()


def can_approve(user: models.User, request: models.SupplyRequest) -> bool:
    if user.role not in ("keeper", "director"):
        return False
    if user.id == request.applicant_id:
        return False
    return True
