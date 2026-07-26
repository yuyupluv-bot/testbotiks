-- PostgreSQL equivalent of Alembic revision 0008_pickup_city_priority.
-- Prefer `alembic upgrade head`; use this file only for manual deployments.

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS pickup_city VARCHAR(120) NULL;

CREATE INDEX IF NOT EXISTS ix_orders_pickup_city
    ON orders (pickup_city);

UPDATE orders
SET pickup_city = line
WHERE pickup_city IS NULL
  AND line IS NOT NULL;
