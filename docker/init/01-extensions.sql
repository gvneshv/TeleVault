-- Enables trigram-based text matching, used to replace SQLite's FTS5 trigram-tokenized full-text search for message search
-- (decided during the Postgres migration - see CHANGELOG "1.2.0": pg_trgm was chosen specifically because Postgres's native tsvector/tsquery search is
-- word-based and can't reproduce the current substring/mid-word/Cyrillic match behaviour).
--
-- Only takes effect on a fresh data volume - Postgres's official image runs everything in /docker-entrypoint-initdb.d/ once, at first init.
CREATE EXTENSION IF NOT EXISTS pg_trgm;