CREATE INDEX IF NOT EXISTS ix_orders_passenger_status_created ON orders (passenger_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_orders_driver_status_created ON orders (driver_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_orders_dispatcher_status_created ON orders (dispatcher_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_driver_queue_status_position ON drivers_queue (status, position);
CREATE INDEX IF NOT EXISTS ix_passenger_queue_status_position ON passenger_queue (status, position);
CREATE INDEX IF NOT EXISTS ix_bookings_status_scheduled ON bookings (status, scheduled_at);
