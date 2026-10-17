-- Runs once, against POSTGRES_DB (the control database, see docker-compose.yml), and only on a fresh data volume -
-- Postgres's official image executes everything in /docker-entrypoint-initdb.d/ at first initialisation, never on later restarts.
--
-- NOTE: the control database does not itself use pg_trgm. Trigram search lives in the per-account ARCHIVE databases,
-- and those get the extension from their own baseline Alembic migration (CREATE EXTENSION IF NOT EXISTS pg_trgm), so provisioning works on any Postgres,
-- with or without this file.
-- This script is kept only so that a developer poking around in the control DB with psql finds the extension available; it is safe to delete.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
