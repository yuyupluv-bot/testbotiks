CREATE TABLE IF NOT EXISTS bookings (
    id SERIAL PRIMARY KEY,
    passenger_id INTEGER NOT NULL REFERENCES users(id),
    driver_id INTEGER REFERENCES users(id),
    order_id INTEGER UNIQUE REFERENCES orders(id),
    type VARCHAR(30) NOT NULL CHECK (type IN ('far_distance', 'early_time')),
    scheduled_time TIME NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    from_address VARCHAR(500) NOT NULL,
    to_address VARCHAR(500) NOT NULL DEFAULT '',
    route_text TEXT NOT NULL,
    extra_services TEXT,
    comment TEXT NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'assigned', 'driver_en_route', 'completed', 'canceled')),
    canceled_by VARCHAR(20),
    reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_bookings_passenger_id ON bookings(passenger_id);
CREATE INDEX IF NOT EXISTS ix_bookings_driver_id ON bookings(driver_id);
CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings(status);
CREATE INDEX IF NOT EXISTS ix_bookings_scheduled_at ON bookings(scheduled_at);
