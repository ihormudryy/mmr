"""P4 — paper/live canary promotion evidence and stage machine.

See ``trader/promotion/evidence_store.py`` for durable evidence ingestion
and derived-window projection, and ``trader/promotion/stage.py`` for the
promotion stage state machine. Journal migrations 40-49 are owned by this
plan (40-42 land in Task 1; see ``trader/data/schema_migrations.py``).
"""
