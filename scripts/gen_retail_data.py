"""生成演示业务库 test_data/data/retail.db（确定性 seed=42）。

数据故事（IMPLEMENTATION_SPEC.md 第 4 节，必须成立）：
- 区域月度销量基准（completed 且非测试渠道的 quantity 合计）：
  HD 华东 ≈1300 / HB 华北 ≈900 / HN 华南 ≈800 / XB 西北 ≈500，随机噪声 ±5%
- 2024-04 华东下滑 ~35%（其中产品线 A 占跌幅 ~75%）
- 2024-05 华东回升 +25%
- 其余区域正常波动

运行：python scripts/gen_retail_data.py
结尾打印 月度×区域 销量透视表与 2024-04 华东产品线拆分，供肉眼验证数据故事。
"""

import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台强制 UTF-8 输出

from chatbi.config import BIZ_DB_PATH, ensure_dirs  # noqa: E402

SEED = 42

# ---------- 区域结构：每区 2 省 × 每省 2 市 × 每市 2 店 = 32 店 ----------
REGIONS: dict[str, dict] = {
    "HD": {"name": "华东", "base": 1300, "provinces": [("江苏", ["南京", "苏州"]), ("浙江", ["杭州", "宁波"])]},
    "HB": {"name": "华北", "base": 900, "provinces": [("河北", ["石家庄", "唐山"]), ("山西", ["太原", "大同"])]},
    "HN": {"name": "华南", "base": 800, "provinces": [("广东", ["广州", "深圳"]), ("福建", ["福州", "厦门"])]},
    "XB": {"name": "西北", "base": 500, "provinces": [("陕西", ["西安", "咸阳"]), ("甘肃", ["兰州", "天水"])]},
}

LINE_RATIO = {"A": 0.40, "B": 0.35, "C": 0.25}   # 产品线销量基准占比
LINE_PRICE = {"A": 100.0, "B": 200.0, "C": 150.0}  # 产品线基准单价（元）

# 华东特殊月份的产品线目标（正式口径：completed 且 channel<>'TEST' 的 quantity 合计）：
# 2024-04 总 845 = 基准 ×0.65（-35%）；跌幅 455 中 A 承担 341（≈75%）、B/C 各 57
# 2024-05 总 1056 = 845 ×1.25（+25%）；其中 A 恢复 148（占回升 70%，供归因下钻指向产品线A）
SPECIAL_HD = {
    "2024-04": {"A": 179, "B": 398, "C": 268},
    "2024-05": {"A": 327, "B": 430, "C": 299},
}

MONTHS = [f"{y}-{m:02d}" for y, m in
          [(2023, m) for m in range(6, 13)] + [(2024, m) for m in range(1, 7)]]
MONTH_DAYS = {"2023-06": 30, "2023-07": 31, "2023-08": 31, "2023-09": 30, "2023-10": 31,
              "2023-11": 30, "2023-12": 31, "2024-01": 31, "2024-02": 29, "2024-03": 31,
              "2024-04": 30, "2024-05": 31, "2024-06": 30}

N_CUSTOMERS = 200


def _month_line_target(region: str, ym: str, line: str, rng: random.Random) -> int:
    """该 月×区×产品线 的目标销量（正式口径）。华东特殊月用查表值，其余基准×占比×噪声。"""
    if region == "HD" and ym in SPECIAL_HD:
        return SPECIAL_HD[ym][line]
    base = REGIONS[region]["base"] * LINE_RATIO[line]
    return int(round(base * rng.uniform(0.95, 1.05)))


def _pick_status(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.85:
        return "completed"
    return "refunded" if r < 0.93 else "cancelled"


def _pick_channel(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.03:
        return "TEST"
    return "ONLINE" if r < 0.58 else "OFFLINE"


def build_stores() -> list[tuple]:
    """32 家门店：(store_id, store_name, region_name, province, city)。"""
    stores = []
    seq = 0
    for region, info in REGIONS.items():
        for province, cities in info["provinces"]:
            for city in cities:
                for j in (1, 2):
                    seq += 1
                    stores.append((f"S{seq:02d}", f"{city}{j}号店", region, province, city))
    return stores


def build_customers(rng: random.Random) -> list[tuple]:
    """200 个客户；~45% 首单在 2022-01~2023-03（演示"老客户"口径），其余更晚。"""
    customers = []
    for i in range(1, N_CUSTOMERS + 1):
        if rng.random() < 0.45:
            first = f"{rng.randint(2022, 2022)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
            if rng.random() < 0.3:
                first = f"2023-{rng.randint(1, 3):02d}-{rng.randint(1, 28):02d}"
        else:
            first = f"2023-{rng.randint(4, 12):02d}-{rng.randint(1, 28):02d}"
            if rng.random() < 0.4:
                first = f"2024-{rng.randint(1, 5):02d}-{rng.randint(1, 28):02d}"
        r = rng.random()
        level = "A" if r < 0.2 else ("B" if r < 0.7 else "C")
        customers.append((f"C{i:03d}", first, level))
    return customers


def gen_data() -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
    """生成全部数据，返回 (stores, customers, orders, order_items)。"""
    rng = random.Random(SEED)
    stores = build_stores()
    customers = build_customers(rng)
    stores_by_region = {r: [s[0] for s in stores if s[2] == r] for r in REGIONS}

    orders: list[tuple] = []
    items: list[tuple] = []
    order_seq = 0

    for ym in MONTHS:  # 固定遍历顺序保证确定性
        days = MONTH_DAYS[ym]
        for region in REGIONS:
            for line in ("A", "B", "C"):
                target = _month_line_target(region, ym, line, rng)
                produced = 0  # 只累计正式口径（completed 且非 TEST）的销量
                while produced < target:
                    order_seq += 1
                    order_id = f"O{order_seq:06d}"
                    status = _pick_status(rng)
                    channel = _pick_channel(rng)
                    order_date = f"{ym}-{rng.randint(1, days):02d}"
                    store_id = rng.choice(stores_by_region[region])
                    customer_id = f"C{rng.randint(1, N_CUSTOMERS):03d}"
                    n_lines = rng.randint(1, 3)
                    order_qty = 0
                    for _ in range(n_lines):
                        qty = rng.randint(1, 5)
                        price = round(LINE_PRICE[line] * rng.uniform(0.8, 1.2), 2)
                        discount = round(rng.uniform(0.7, 1.0), 2)
                        items.append((order_id, line, qty, price, discount))
                        order_qty += qty
                    orders.append((order_id, store_id, customer_id, status, order_date, channel))
                    if status == "completed" and channel != "TEST":
                        produced += order_qty
    return stores, customers, orders, items


def write_db(stores, customers, orders, items) -> None:
    ensure_dirs()
    if BIZ_DB_PATH.exists():
        BIZ_DB_PATH.unlink()  # 全量重建，保证确定性
    conn = sqlite3.connect(str(BIZ_DB_PATH))
    try:
        conn.executescript("""
            CREATE TABLE stores(
                store_id   TEXT PRIMARY KEY,
                store_name TEXT NOT NULL,
                region_name TEXT NOT NULL,
                province   TEXT NOT NULL,
                city       TEXT NOT NULL
            );
            CREATE TABLE customers(
                customer_id      TEXT PRIMARY KEY,
                first_order_date TEXT NOT NULL,
                level            TEXT NOT NULL
            );
            CREATE TABLE orders(
                order_id    TEXT PRIMARY KEY,
                store_id    TEXT NOT NULL REFERENCES stores(store_id),
                customer_id TEXT NOT NULL REFERENCES customers(customer_id),
                status      TEXT NOT NULL,
                order_date  TEXT NOT NULL,
                channel     TEXT NOT NULL
            );
            CREATE TABLE order_items(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id      TEXT NOT NULL REFERENCES orders(order_id),
                product_line  TEXT NOT NULL,
                quantity      INTEGER NOT NULL,
                unit_price    REAL NOT NULL,
                discount_rate REAL NOT NULL
            );
            CREATE INDEX idx_orders_date ON orders(order_date);
            CREATE INDEX idx_orders_store ON orders(store_id);
            CREATE INDEX idx_items_order ON order_items(order_id);
        """)
        conn.executemany("INSERT INTO stores VALUES (?,?,?,?,?)", stores)
        conn.executemany("INSERT INTO customers VALUES (?,?,?)", customers)
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", orders)
        conn.executemany(
            "INSERT INTO order_items(order_id, product_line, quantity, unit_price, discount_rate) VALUES (?,?,?,?,?)",
            items,
        )
        conn.commit()
    finally:
        conn.close()


def print_pivot(conn: sqlite3.Connection) -> None:
    """打印 月度×区域 销量透视表（正式口径）+ 2024-04/05 华东产品线拆分，自证数据故事。"""
    print("\n===== 月度×区域 销量透视表（口径：status='completed' 且 channel<>'TEST'，单位：件） =====")
    regions = list(REGIONS.keys())
    header = "月份    | " + " | ".join(f"{REGIONS[r]['name']:^6}" for r in regions) + " |  全国合计"
    print(header)
    print("-" * len(header.encode("gbk", errors="ignore")) )
    monthly: dict[str, dict[str, int]] = {}
    for ym in MONTHS:
        row: dict[str, int] = {}
        for region in regions:
            qty = conn.execute(
                """
                SELECT COALESCE(SUM(oi.quantity), 0)
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.order_id
                JOIN stores s ON o.store_id = s.store_id
                WHERE o.status = 'completed' AND o.channel <> 'TEST'
                  AND substr(o.order_date, 1, 7) = ? AND s.region_name = ?
                """,
                (ym, region),
            ).fetchone()[0]
            row[region] = qty
        monthly[ym] = row
        print(f"{ym} | " + " | ".join(f"{row[r]:^6d}" for r in regions) + f" |  {sum(row.values()):^6d}")

    def pct(a: int, b: int) -> str:
        return f"{(a - b) / b * 100:+.1f}%" if b else "n/a"

    hd = monthly["2024-04"]["HD"]
    print(f"\n数据故事自检（华东 HD）:")
    print(f"  2024-04 环比 2024-03 : {pct(monthly['2024-04']['HD'], monthly['2024-03']['HD'])}（预期 ≈ -35%）")
    print(f"  2024-05 环比 2024-04 : {pct(monthly['2024-05']['HD'], hd)}（预期 ≈ +25%）")
    for r in ("HB", "HN", "XB"):
        print(f"  其余区域示例 {REGIONS[r]['name']} 2024-04 环比 : "
              f"{pct(monthly['2024-04'][r], monthly['2024-03'][r])}（预期为小幅噪声）")

    print("\n===== 华东 2024-03/04/05 按产品线销量（自证 A 线占跌幅 ~75%） =====")
    print("产品线 | 2024-03 | 2024-04 | 2024-05 | 4月较3月变化 | 5月较4月变化")
    for line in ("A", "B", "C"):
        vals = {}
        for ym in ("2024-03", "2024-04", "2024-05"):
            vals[ym] = conn.execute(
                """
                SELECT COALESCE(SUM(oi.quantity), 0)
                FROM order_items oi
                JOIN orders o ON oi.order_id = o.order_id
                JOIN stores s ON o.store_id = s.store_id
                WHERE o.status = 'completed' AND o.channel <> 'TEST'
                  AND substr(o.order_date, 1, 7) = ? AND s.region_name = 'HD'
                  AND oi.product_line = ?
                """,
                (ym, line),
            ).fetchone()[0]
        print(f"  {line}   | {vals['2024-03']:7d} | {vals['2024-04']:7d} | {vals['2024-05']:7d} "
              f"| {vals['2024-04'] - vals['2024-03']:+6d}     | {vals['2024-05'] - vals['2024-04']:+6d}")

    # 状态/渠道占比自检
    print("\n===== 订单状态与渠道占比自检 =====")
    total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    for row in conn.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status ORDER BY c DESC"):
        print(f"  status={row[0]:<10} {row[1]:>6d} 单（{row[1] / total * 100:.1f}%）")
    for row in conn.execute("SELECT channel, COUNT(*) c FROM orders GROUP BY channel ORDER BY c DESC"):
        print(f"  channel={row[0]:<8} {row[1]:>6d} 单（{row[1] / total * 100:.1f}%）")


def main() -> None:
    stores, customers, orders, items = gen_data()
    write_db(stores, customers, orders, items)
    print(f"已生成业务库: {BIZ_DB_PATH}")
    print(f"  门店 {len(stores)} | 客户 {len(customers)} | 订单 {len(orders)} | 订单行 {len(items)}")

    conn = sqlite3.connect(str(BIZ_DB_PATH))
    try:
        print_pivot(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
