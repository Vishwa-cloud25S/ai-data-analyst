-- Least-privilege role used by the API when WAREHOUSE=postgres.
-- The application role can read the three certified marts and nothing else.

CREATE SCHEMA IF NOT EXISTS main;

CREATE TABLE IF NOT EXISTS main.dim_products (
    product_id   varchar PRIMARY KEY,
    product_name varchar,
    category     varchar,
    brand        varchar,
    list_price   numeric(10,2),
    is_active    boolean
);

CREATE TABLE IF NOT EXISTS main.dim_customers (
    customer_id   varchar PRIMARY KEY,
    customer_name varchar,
    segment       varchar,
    country       varchar,
    signup_date   date
);

CREATE TABLE IF NOT EXISTS main.fct_orders (
    order_line_id   varchar PRIMARY KEY,
    order_id        varchar,
    order_date      date,
    order_status    varchar,
    channel         varchar,
    region          varchar,
    customer_id     varchar,
    product_id      varchar,
    quantity        integer,
    unit_price      numeric(10,2),
    discount_amount numeric(10,2),
    net_revenue     numeric(12,2),
    cost_amount     numeric(12,2)
);

-- Sensitive table that is deliberately NOT exposed in the semantic layer.
CREATE TABLE IF NOT EXISTS main.employee_salaries (
    employee_id varchar PRIMARY KEY,
    full_name   varchar,
    salary_usd  numeric(12,2)
);

CREATE INDEX IF NOT EXISTS idx_fct_orders_date ON main.fct_orders (order_date);
CREATE INDEX IF NOT EXISTS idx_fct_orders_product ON main.fct_orders (product_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_ro') THEN
        CREATE ROLE analyst_ro LOGIN PASSWORD 'analyst_ro';
    END IF;
END $$;

-- Defence in depth: the database itself refuses writes for this role.
ALTER ROLE analyst_ro SET default_transaction_read_only = on;
ALTER ROLE analyst_ro SET statement_timeout = '20s';

REVOKE ALL ON ALL TABLES IN SCHEMA main FROM analyst_ro;
GRANT USAGE ON SCHEMA main TO analyst_ro;
GRANT SELECT ON main.fct_orders, main.dim_products, main.dim_customers TO analyst_ro;
-- employee_salaries is intentionally never granted.
