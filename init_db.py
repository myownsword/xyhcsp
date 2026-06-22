from datetime import date, timedelta
from sqlalchemy.orm import Session
from database import SessionLocal, engine, Base
import models
from auth import hash_password


def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(models.User).count() > 0:
            print("数据库已存在数据，跳过初始化")
            return

        print("初始化数据库...")

        users = [
            models.User(
                username="teacher1",
                password=hash_password("123456"),
                full_name="张老师",
                role="teacher",
                department="化学系"
            ),
            models.User(
                username="teacher2",
                password=hash_password("123456"),
                full_name="李老师",
                role="teacher",
                department="生物系"
            ),
            models.User(
                username="keeper1",
                password=hash_password("123456"),
                full_name="王库管",
                role="keeper",
                department="设备处"
            ),
            models.User(
                username="director1",
                password=hash_password("123456"),
                full_name="赵主任",
                role="director",
                department="化学系"
            ),
        ]
        for u in users:
            db.add(u)
        db.flush()
        print(f"创建了 {len(users)} 个用户")

        supplies = [
            models.Supply(
                name="浓硫酸",
                category="reagent",
                unit="瓶",
                unit_price=85.0,
                is_high_risk=True,
                risk_level="high",
                specification="500mL/分析纯",
                description="腐蚀性强酸，需特别注意安全"
            ),
            models.Supply(
                name="无水乙醇",
                category="reagent",
                unit="瓶",
                unit_price=32.0,
                is_high_risk=True,
                risk_level="medium",
                specification="500mL/分析纯",
                description="易燃有机溶剂"
            ),
            models.Supply(
                name="氯化钠",
                category="reagent",
                unit="瓶",
                unit_price=25.0,
                is_high_risk=False,
                risk_level="low",
                specification="500g/分析纯",
                description="普通化学试剂"
            ),
            models.Supply(
                name="丁腈手套",
                category="glove",
                unit="盒",
                unit_price=45.0,
                is_high_risk=False,
                risk_level="low",
                specification="100只/盒 M码",
                description="一次性防护手套"
            ),
            models.Supply(
                name="一次性培养皿",
                category="dish",
                unit="包",
                unit_price=28.0,
                is_high_risk=False,
                risk_level="low",
                specification="90mm 10套/包",
                description="塑料灭菌培养皿"
            ),
            models.Supply(
                name="玻璃培养皿",
                category="dish",
                unit="包",
                unit_price=68.0,
                is_high_risk=False,
                risk_level="low",
                specification="90mm 10套/包",
                description="可重复使用玻璃培养皿"
            ),
            models.Supply(
                name="氢氧化钠",
                category="reagent",
                unit="瓶",
                unit_price=38.0,
                is_high_risk=True,
                risk_level="medium",
                specification="500g/分析纯",
                description="强碱，有腐蚀性"
            ),
        ]
        for s in supplies:
            db.add(s)
        db.flush()
        print(f"创建了 {len(supplies)} 种耗材")

        today = date.today()

        batches_data = [
            (1, "H2SO4-2025-001", today - timedelta(days=30), today + timedelta(days=720), 20, "国药集团", "A区危险品柜1号"),
            (1, "H2SO4-2024-089", today - timedelta(days=180), today + timedelta(days=30), 10, "国药集团", "A区危险品柜1号"),
            (1, "H2SO4-2022-156", today - timedelta(days=900), today - timedelta(days=30), 5, "国药集团", "A区过期区"),
            (2, "C2H5OH-2025-023", today - timedelta(days=20), today + timedelta(days=540), 50, "西陇化工", "B区易燃品柜"),
            (2, "C2H5OH-2024-150", today - timedelta(days=200), today + timedelta(days=90), 30, "西陇化工", "B区易燃品柜"),
            (3, "NaCl-2025-012", today - timedelta(days=15), today + timedelta(days=1080), 100, "国药集团", "C区普通货架"),
            (4, "GLOVE-M-202502", today - timedelta(days=45), today + timedelta(days=1080), 200, "英科医疗", "D区耗材区"),
            (5, "DISH-90P-202501", today - timedelta(days=60), today + timedelta(days=1800), 150, "赛默飞", "D区耗材区"),
            (6, "DISH-90G-202406", today - timedelta(days=120), today + timedelta(days=3600), 80, "蜀牛", "D区耗材区"),
            (7, "NaOH-2025-007", today - timedelta(days=25), today + timedelta(days=720), 25, "国药集团", "A区危险品柜2号"),
        ]

        batches = []
        for supply_id, batch_no, prod_date, exp_date, qty, supplier, location in batches_data:
            batches.append(models.SupplyBatch(
                supply_id=supply_id,
                batch_no=batch_no,
                production_date=prod_date,
                expiry_date=exp_date,
                total_quantity=qty,
                available_quantity=qty,
                frozen_quantity=0,
                supplier=supplier,
                storage_location=location
            ))
        for b in batches:
            db.add(b)
        db.flush()

        u_id = 3  # keeper1的ID
        for b in batches:
            txn = models.InventoryTransaction(
                batch_id=b.id,
                transaction_type="in",
                quantity=b.total_quantity,
                balance_before=0.0,
                balance_after=b.available_quantity,
                frozen_before=0,
                frozen_after=0,
                reference_type="manual",
                reference_id=None,
                operator_id=u_id,
                remark=f"初始入库：批次{b.batch_no}"
            )
            db.add(txn)
        db.flush()
        print(f"创建了 {len(batches)} 个批次")

        budgets = [
            models.BudgetRecord(
                department="化学系",
                fiscal_year=today.year,
                total_budget=50000.0,
                used_budget=0.0
            ),
            models.BudgetRecord(
                department="生物系",
                fiscal_year=today.year,
                total_budget=30000.0,
                used_budget=0.0
            ),
        ]
        for b in budgets:
            db.add(b)
        db.flush()
        print(f"创建了 {len(budgets)} 条预算记录")

        db.commit()
        print("初始化完成！")
        print()
        print("测试账号：")
        print("  教师：teacher1 / 123456 (张老师 - 化学系)")
        print("  教师：teacher2 / 123456 (李老师 - 生物系)")
        print("  库管：keeper1 / 123456 (王库管)")
        print("  主任：director1 / 123456 (赵主任 - 化学系)")

    except Exception as e:
        db.rollback()
        print(f"初始化失败: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
