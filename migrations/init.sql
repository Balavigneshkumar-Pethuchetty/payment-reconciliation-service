-- Bootstrap script (SQLAlchemy auto-creates tables via create_all on startup)
-- Run this only if you prefer manual schema management over auto-create.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Immutable audit trigger: prevent UPDATE/DELETE on audit_logs
CREATE OR REPLACE FUNCTION prevent_audit_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only: % on % is not allowed', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

-- Apply after tables are created by SQLAlchemy
-- (Run this after first startup)
--
-- CREATE TRIGGER audit_immutable
--     BEFORE UPDATE OR DELETE ON audit_logs
--     FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();
