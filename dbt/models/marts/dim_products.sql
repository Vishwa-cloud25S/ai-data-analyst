{{ config(materialized='table') }}

select
    product_id,
    product_name,
    category,
    brand,
    cast(list_price as decimal(10,2)) as list_price,
    coalesce(is_active, true)         as is_active
from {{ source('raw', 'products') }}
