{{ config(materialized='view') }}

select
    order_line_id,
    order_id,
    product_id,
    cast(quantity as integer)         as quantity,
    cast(unit_price as decimal(10,2)) as unit_price,
    cast(unit_cost as decimal(10,2))  as unit_cost,
    coalesce(cast(discount_amount as decimal(10,2)), 0) as discount_amount
from {{ source('raw', 'order_lines') }}
