"""Build the local DuckDB warehouse with deterministic synthetic retail data.

Run:  python -m app.db.seed
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

from app.core.config import settings

SEED = 42

CATEGORIES = {
    "Audio": [("Aurora Wireless Earbuds", "Nimbus", 149.0, 58.0),
              ("Aurora Studio Headphones", "Nimbus", 299.0, 121.0),
              ("Pebble Bluetooth Speaker", "Kestrel", 89.0, 33.0)],
    "Computing": [("Vertex 14 Laptop", "Vertex", 1399.0, 940.0),
                  ("Vertex 16 Pro Laptop", "Vertex", 2199.0, 1520.0),
                  ("Mono Mechanical Keyboard", "Kestrel", 129.0, 47.0),
                  ("Glide Wireless Mouse", "Kestrel", 59.0, 19.0)],
    "Wearables": [("Halo Smartwatch 2", "Nimbus", 349.0, 138.0),
                  ("Halo Fitness Band", "Nimbus", 99.0, 31.0)],
    "Home": [("Lumen Smart Lamp", "Lumen", 79.0, 26.0),
             ("Lumen Air Purifier", "Lumen", 259.0, 112.0)],
    "Accessories": [("Trek Laptop Sleeve", "Trek", 45.0, 12.0),
                    ("Trek Travel Charger", "Trek", 69.0, 21.0),
                    ("Trek USB-C Hub", "Trek", 89.0, 28.0)],
}

SEGMENTS = ["Consumer", "SMB", "Enterprise"]
COUNTRIES = {"NA": ["US", "CA"], "EMEA": ["GB", "DE", "FR"],
             "APAC": ["IN", "JP", "AU"], "LATAM": ["BR", "MX"]}
CHANNELS = ["web", "mobile", "retail", "partner"]
STATUSES = ["delivered"] * 70 + ["shipped"] * 15 + ["placed"] * 7 + ["returned"] * 5 + ["cancelled"] * 3

DDL = """
create table dim_products (
    product_id   varchar primary key,
    product_name varchar,
    category     varchar,
    brand        varchar,
    list_price   decimal(10,2),
    is_active    boolean
);
create table dim_customers (
    customer_id   varchar primary key,
    customer_name varchar,
    segment       varchar,
    country       varchar,
    signup_date   date
);
create table fct_orders (
    order_line_id   varchar primary key,
    order_id        varchar,
    order_date      date,
    order_status    varchar,
    channel         varchar,
    region          varchar,
    customer_id     varchar,
    product_id      varchar,
    quantity        integer,
    unit_price      decimal(10,2),
    discount_amount decimal(10,2),
    net_revenue     decimal(12,2),
    cost_amount     decimal(12,2)
);
-- Intentionally NOT in the semantic layer: proves the LLM cannot touch it.
create table employee_salaries (
    employee_id varchar primary key,
    full_name   varchar,
    salary_usd  decimal(12,2)
);
"""


def build(db_path: str | None = None, days: int = 730, n_orders: int = 6000) -> str:
    path = Path(db_path or settings.duckdb_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    rng = random.Random(SEED)
    con = duckdb.connect(str(path))
    con.execute(DDL)

    products = []
    pid = 0
    for category, items in CATEGORIES.items():
        for name, brand, price, cost in items:
            pid += 1
            products.append((f"P{pid:03d}", name, category, brand, price, cost, True))
    con.executemany(
        "insert into dim_products values (?,?,?,?,?,?)",
        [(p[0], p[1], p[2], p[3], p[4], p[6]) for p in products],
    )

    customers = []
    today = date.today()
    for i in range(1, 801):
        region = rng.choice(list(COUNTRIES))
        customers.append((
            f"C{i:04d}", f"Customer {i:04d}", rng.choice(SEGMENTS),
            rng.choice(COUNTRIES[region]),
            today - timedelta(days=rng.randint(30, 1500)),
        ))
    con.executemany("insert into dim_customers values (?,?,?,?,?)", customers)

    country_to_region = {c: r for r, cs in COUNTRIES.items() for c in cs}
    # Weight products so the ranking is stable and interesting.
    weights = [3 if p[2] in ("Audio", "Computing") else 2 for p in products]

    lines, line_no = [], 0
    for o in range(1, n_orders + 1):
        cust = rng.choice(customers)
        order_date = today - timedelta(days=rng.randint(0, days))
        # Mild upward trend + Q4 seasonality.
        status = rng.choice(STATUSES)
        channel = rng.choice(CHANNELS)
        order_id = f"O{o:06d}"
        for _ in range(rng.randint(1, 3)):
            line_no += 1
            prod = rng.choices(products, weights=weights, k=1)[0]
            qty = rng.randint(1, 4)
            seasonal = 1.15 if order_date.month in (11, 12) else 1.0
            trend = 1 + (days - (today - order_date).days) / (days * 4)
            unit_price = round(prod[4] * rng.uniform(0.95, 1.05) * seasonal * trend, 2)
            discount = round(unit_price * qty * rng.choice([0, 0, 0, 0.05, 0.1, 0.15]), 2)
            net = round(unit_price * qty - discount, 2)
            cost = round(prod[5] * qty, 2)
            lines.append((
                f"L{line_no:07d}", order_id, order_date, status, channel,
                country_to_region[cust[3]], cust[0], prod[0], qty,
                unit_price, discount, net, cost,
            ))
    con.executemany("insert into fct_orders values (?,?,?,?,?,?,?,?,?,?,?,?,?)", lines)

    con.executemany(
        "insert into employee_salaries values (?,?,?)",
        [(f"E{i:03d}", f"Employee {i:03d}", 60000 + i * 137) for i in range(1, 51)],
    )
    con.close()
    return str(path)


if __name__ == "__main__":  # pragma: no cover
    p = build()
    print(f"warehouse built at {p}")
