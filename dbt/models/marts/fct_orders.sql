{{ config(materialized='table') }}

-- Order-line grain revenue fact. net_revenue is pre-computed so that every
-- downstream consumer (including the AI analyst) agrees on the definition.
with lines as (
    select * from {{ ref('stg_order_lines') }}
),
orders as (
    select * from {{ ref('stg_orders') }}
)
select
    l.order_line_id,
    o.order_id,
    o.order_date,
    o.order_status,
    o.channel,
    o.region,
    o.customer_id,
    l.product_id,
    l.quantity,
    l.unit_price,
    l.discount_amount,
    l.quantity * l.unit_price - l.discount_amount as net_revenue,
    l.quantity * l.unit_cost                       as cost_amount
from lines l
join orders o using (order_id)
