ALTER TABLE orders
ADD COLUMN IF NOT EXISTS parallel_notified_driver_ids TEXT;
