-- Season settings per hotel (seasonal resorts).
-- Captured at onboarding: opening/closing dates for LAST year and THIS year,
-- so occupancy denominators can use OPEN days instead of calendar days, and
-- the closed-season hero knows the real season window.
-- total_rooms (available inventory) already exists on hotels — verify it at
-- onboarding, do not assume the PMS value.
--
-- Example value:
--   {"2025": {"open": "2025-04-18", "close": "2025-11-02"},
--    "2026": {"open": "2026-04-15", "close": "2026-11-01"}}
--
-- Code must tolerate this column not existing yet (schema-tolerant reads).

alter table hotels add column if not exists season_settings jsonb;
