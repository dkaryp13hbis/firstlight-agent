-- 2026-10-28 — Demo account switches to anonymized twins of the Tor group
-- (user: "lets switch the demo to the tor hotel group, so we will have both
-- multi and standalone properties with full data").
--
-- Creates one demo group → one demo company → three demo hotels, each a twin
-- of a live Tor hotel (pms_config.demo_of = real hotel id). Twins are
-- active=false: the scheduler, the audit and /health never touch them;
-- db/store treats them as visible (memberships + portfolio). Their briefings
-- are copied nightly by briefing/demo_sync.py with the "replace" pairs below.
-- The demo login gets the demo GROUP (= three standalone hotels + the
-- portfolio entry) and loses the resort twins Azure Bay / Thalassa (rows and
-- history are kept, only the access goes).
--
-- DML only, idempotent (keyed on slugs). Run AFTER the backend that reads
-- pms_config.demo_of is live. Prints its own verification:
--   powershell -ExecutionPolicy Bypass -File scripts\pg_migrate.ps1 docs\sql\pg\2026-10-28_demo_tor_twins.sql

insert into groups (name, slug) values ('Aurora Hotels', 'demo-aurora')
on conflict (slug) do update set name = excluded.name;

insert into organizations (group_id, name, slug, country)
select g.id, 'Aurora Hotels (demo)', 'demo-aurora-hotels', 'GR'
  from groups g where g.slug = 'demo-aurora'
on conflict (slug) do update set group_id = excluded.group_id, name = excluded.name;

-- Text pairs applied to every copied briefing (longest phrase first, one pass).
-- The exact source name is added per hotel below; these catch short forms and
-- the city / group names. Extend here when the verification below shows a
-- name that should not reach the demo (agencies, companies), then re-run.
with shared(pairs) as (values (jsonb_build_array(
        jsonb_build_array('City Hotel Thessaloniki', 'Aurora City Hotel'),
        jsonb_build_array('City Hotel',              'Aurora City Hotel'),
        jsonb_build_array('The Excelsior Hotel',     'Aurora Boutique Hotel'),
        jsonb_build_array('The Excelsior',           'Aurora Boutique Hotel'),
        jsonb_build_array('Excelsior',               'Aurora Boutique'),
        jsonb_build_array('ON Residence',            'Aurora Residences'),
        jsonb_build_array('Tor City Hotels Group',   'Aurora Hotels'),
        jsonb_build_array('Tor Hotel Group',         'Aurora Hotels'),
        jsonb_build_array('Thessaloniki',            'the city')))),
src as (
  select h.id, h.name, h.total_rooms, h.timezone, h.pms_type, h.season_settings,
         v.demo_name, v.demo_slug
    from (values
      ('City Hotel%',    'Aurora City Hotel',     'demo-aurora-city'),
      ('%Excelsior%',    'Aurora Boutique Hotel', 'demo-aurora-boutique'),
      ('ON Residence%',  'Aurora Residences',     'demo-aurora-residences')
    ) as v(match, demo_name, demo_slug)
    join hotels h on h.name ilike v.match and h.active
               and h.pms_config->>'tunnel_hostname' = 'sql-torcity.hbis.io'
)
insert into hotels (org_id, name, slug, total_rooms, timezone, pms_type, active,
                    pms_config, season_settings)
select o.id, s.demo_name, s.demo_slug, s.total_rooms, s.timezone, s.pms_type, false,
       jsonb_build_object(
         'demo_of', s.id::text,
         'place',   '',
         'replace', jsonb_build_array(jsonb_build_array(s.name, s.demo_name)) || sh.pairs),
       s.season_settings
  from src s
  cross join shared sh
  join organizations o on o.slug = 'demo-aurora-hotels'
on conflict (slug) do update
   set name = excluded.name, total_rooms = excluded.total_rooms,
       pms_config = excluded.pms_config, season_settings = excluded.season_settings,
       active = false;

-- demo login: viewer of the demo group (only if demo@hbis.io is already on our
-- login — otherwise create it first, see the log entry of 2026-10-28)
insert into memberships (user_id, scope_type, scope_id, role)
select u.id, 'group', g.id, 'viewer'
  from users u join groups g on g.slug = 'demo-aurora'
 where lower(u.email) = 'demo@hbis.io'
on conflict (user_id, scope_type, scope_id) do nothing;

-- …and no longer the resort twins (Azure Bay, Thalassa)
delete from memberships m using users u
 where m.user_id = u.id and lower(u.email) = 'demo@hbis.io'
   and m.scope_type = 'hotel'
   and m.scope_id in ('beaed5c5-3af6-4dde-b0b9-ade606951978',
                      '18dd1171-f66f-401c-af77-3132bb9b52a4');

-- ── verify ──────────────────────────────────────────────────────────────────
-- expect 3 rows: each twin next to its live Tor source
select t.name as demo_hotel, s.name as copies, t.total_rooms, t.active,
       jsonb_array_length(t.pms_config->'replace') as pairs
  from hotels t join hotels s on s.id::text = t.pms_config->>'demo_of'
 order by t.name;

-- expect the 3 Aurora hotels and nothing else
select u.email, h.name, a.role, a.via
  from hotel_access a join users u on u.id = a.user_id join hotels h on h.id = a.hotel_id
 where lower(u.email) = 'demo@hbis.io' order by h.name;
