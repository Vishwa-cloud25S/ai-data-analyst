{{ config(materialized='view') }}

select
    order_id,
    cast(order_date as date)      as order_date,
    lower(order_status)           as order_status,
    lower(channel)                as channel,
    upper(region)                 as region,
    customer_id
from {{ source('raw', 'orders') }}
where order_id is not null
