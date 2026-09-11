-- 2026-09-12 — Tor group: morning AI briefing at 05:00 Europe/Athens.
-- railway_main runs a slot="early" full briefing at 05:00 Athens (DST-proof);
-- hotels opt in via pms_config.briefing_slot = "early".  The global 03:30 /
-- 06:00 UTC runs remain their catch-up (skip at zero cost when the early run
-- succeeded).  Idempotent; keyed on the group's shared tunnel hostname.

UPDATE hotels
   SET pms_config = jsonb_set(pms_config, '{briefing_slot}', '"early"', true)
 WHERE pms_config->>'tunnel_hostname' = 'sql-torcity.hbis.io';

-- verify
SELECT name, pms_config->>'briefing_slot' AS briefing_slot
  FROM hotels
 WHERE pms_config->>'tunnel_hostname' = 'sql-torcity.hbis.io';
