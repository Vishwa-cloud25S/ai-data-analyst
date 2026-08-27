{{ config(materialized='table') }}

select
    customer_id,
    customer_name,
    segment,
    upper(country)              as country,
    cast(signup_date as date)   as signup_date
from {{ source('raw', 'customers') }}
