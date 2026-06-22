from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, ForeignKey, Text, Boolean
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password = Column(String(100), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(20), nullable=False)  # teacher, keeper, director
    department = Column(String(100), default="")
    created_at = Column(DateTime, default=datetime.now)

    created_plans = relationship("ExperimentPlan", back_populates="creator")
    submitted_requests = relationship("SupplyRequest", back_populates="applicant")
    approvals = relationship("ApprovalRecord", back_populates="approver")


class Supply(Base):
    __tablename__ = "supplies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False)  # reagent, glove, dish, etc.
    unit = Column(String(20), nullable=False)  # 瓶, 盒, 个, mL, etc.
    unit_price = Column(Float, nullable=False, default=0.0)
    is_high_risk = Column(Boolean, default=False)
    risk_level = Column(String(20), default="low")  # low, medium, high
    specification = Column(String(200), default="")
    description = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)

    batches = relationship("SupplyBatch", back_populates="supply", cascade="all, delete-orphan")
    request_items = relationship("RequestItem", back_populates="supply")


class SupplyBatch(Base):
    __tablename__ = "supply_batches"

    id = Column(Integer, primary_key=True, index=True)
    supply_id = Column(Integer, ForeignKey("supplies.id"), nullable=False)
    batch_no = Column(String(50), nullable=False)
    production_date = Column(Date, nullable=False)
    expiry_date = Column(Date, nullable=False)
    total_quantity = Column(Float, nullable=False, default=0)
    available_quantity = Column(Float, nullable=False, default=0)
    frozen_quantity = Column(Float, nullable=False, default=0)
    supplier = Column(String(200), default="")
    storage_location = Column(String(200), default="")
    remark = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)

    supply = relationship("Supply", back_populates="batches")
    transactions = relationship("InventoryTransaction", back_populates="batch")


class ExperimentPlan(Base):
    __tablename__ = "experiment_plans"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    course_name = Column(String(200), default="")
    class_name = Column(String(100), default="")
    student_count = Column(Integer, default=0)
    plan_date = Column(Date, nullable=False)
    description = Column(Text, default="")
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    creator = relationship("User", back_populates="created_plans")
    requests = relationship("SupplyRequest", back_populates="plan")


class SupplyRequest(Base):
    __tablename__ = "supply_requests"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("experiment_plans.id"), nullable=False)
    applicant_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    # pending, approved, partially_approved, rejected, completed
    urgency = Column(String(20), default="normal")  # normal, urgent, emergency
    total_amount = Column(Float, default=0.0)
    remark = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    plan = relationship("ExperimentPlan", back_populates="requests")
    applicant = relationship("User", back_populates="submitted_requests")
    items = relationship("RequestItem", back_populates="request", cascade="all, delete-orphan")
    approvals = relationship("ApprovalRecord", back_populates="request", cascade="all, delete-orphan")
    issuances = relationship("SupplyIssuance", back_populates="request", cascade="all, delete-orphan")


class RequestItem(Base):
    __tablename__ = "request_items"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("supply_requests.id"), nullable=False)
    supply_id = Column(Integer, ForeignKey("supplies.id"), nullable=False)
    requested_quantity = Column(Float, nullable=False)
    approved_quantity = Column(Float, default=0)
    issued_quantity = Column(Float, default=0)
    unit_price_at_approval = Column(Float, default=0.0)
    remark = Column(Text, default="")

    request = relationship("SupplyRequest", back_populates="items")
    supply = relationship("Supply", back_populates="request_items")


class ApprovalRecord(Base):
    __tablename__ = "approval_records"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("supply_requests.id"), nullable=False)
    approver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    step = Column(Integer, nullable=False, default=1)
    decision = Column(String(20), nullable=False)  # approve, partial, reject
    comment = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)

    request = relationship("SupplyRequest", back_populates="approvals")
    approver = relationship("User", back_populates="approvals")


class SupplyIssuance(Base):
    __tablename__ = "supply_issuances"

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("supply_requests.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("request_items.id"), nullable=False)
    batch_id = Column(Integer, ForeignKey("supply_batches.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    keeper_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    issued_at = Column(DateTime, default=datetime.now)
    confirmed = Column(Boolean, default=False)
    confirmed_at = Column(DateTime, nullable=True)
    receiver_name = Column(String(100), default="")
    remark = Column(Text, default="")

    request = relationship("SupplyRequest", back_populates="issuances")
    item = relationship("RequestItem")
    batch = relationship("SupplyBatch")
    keeper = relationship("User")


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("supply_batches.id"), nullable=False)
    transaction_type = Column(String(20), nullable=False)
    # in, out, freeze, unfreeze, adjust
    quantity = Column(Float, nullable=False)
    balance_before = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    frozen_before = Column(Float, default=0)
    frozen_after = Column(Float, default=0)
    reference_type = Column(String(50), default="")  # request, issuance, manual
    reference_id = Column(Integer, nullable=True)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    remark = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)

    batch = relationship("SupplyBatch", back_populates="transactions")
    operator = relationship("User")


class BudgetRecord(Base):
    __tablename__ = "budget_records"

    id = Column(Integer, primary_key=True, index=True)
    department = Column(String(100), nullable=False)
    fiscal_year = Column(Integer, nullable=False)
    total_budget = Column(Float, nullable=False, default=0.0)
    used_budget = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
