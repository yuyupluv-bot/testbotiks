CREATE TABLE IF NOT EXISTS outbox_messages (id SERIAL PRIMARY KEY, peer_id BIGINT NOT NULL, text TEXT, keyboard TEXT, attachment TEXT, random_id BIGINT NOT NULL UNIQUE, status VARCHAR(20) NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), claimed_at TIMESTAMPTZ, sent_at TIMESTAMPTZ, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS processed_events (id SERIAL PRIMARY KEY, event_key VARCHAR(255) NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS scheduled_jobs (id SERIAL PRIMARY KEY, job_key VARCHAR(120) NOT NULL UNIQUE, kind VARCHAR(40) NOT NULL, object_id INTEGER NOT NULL, run_at TIMESTAMPTZ NOT NULL, payload TEXT, status VARCHAR(20) NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS login_attempts (id SERIAL PRIMARY KEY, ip_address VARCHAR(64) NOT NULL, login VARCHAR(120) NOT NULL, success BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox_messages(status,next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_scheduled_jobs_due ON scheduled_jobs(status,run_at);
CREATE INDEX IF NOT EXISTS ix_login_attempts_lookup ON login_attempts(ip_address,login,created_at DESC);
CREATE INDEX IF NOT EXISTS ix_processed_events_created ON processed_events(created_at);
