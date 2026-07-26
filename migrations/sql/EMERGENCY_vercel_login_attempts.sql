-- Emergency fix for the currently deployed Vercel version.
-- Safe to run repeatedly in PostgreSQL/Supabase/Neon.
CREATE TABLE IF NOT EXISTS login_attempts (
    id SERIAL PRIMARY KEY,
    ip_address VARCHAR(64) NOT NULL,
    login VARCHAR(120) NOT NULL,
    success BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_login_attempts_lookup
    ON login_attempts(ip_address, login, created_at DESC);
