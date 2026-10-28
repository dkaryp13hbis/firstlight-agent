-- 2026-10-28 — READ-ONLY check of the demo twins after the first sync
-- (briefing/demo_sync.py). Safe to run any time:
--   powershell -ExecutionPolicy Bypass -File scripts\pg_migrate.ps1 docs\sql\pg\2026-10-28_demo_check.sql

-- 1. history copied: expect ~14 days per twin, newest = its source's newest
select t.name as demo_hotel, count(b.*) as days, max(b.report_date) as newest,
       (select max(report_date) from briefings where hotel_id = s.id) as source_newest
  from hotels t
  join hotels s on s.id::text = t.pms_config->>'demo_of'
  left join briefings b on b.hotel_id = t.id
 group by t.name, s.id order by t.name;

-- 2. leak check: expect 0 everywhere (real hotel name, city, group name)
select t.name as demo_hotel,
       count(*) filter (where (b.data::text || coalesce(b.ai_insights::text, '')) ilike '%' || s.name || '%') as real_name,
       count(*) filter (where (b.data::text || coalesce(b.ai_insights::text, '')) ilike any
                        (array['%Thessaloniki%', '%Θεσσαλον%', '%Excelsior%', '%ON Residence%', '%Tor Hotel%', '%Tor City%'])) as other_names
  from hotels t
  join hotels s on s.id::text = t.pms_config->>'demo_of'
  join briefings b on b.hotel_id = t.id
 group by t.name order by t.name;

-- 3. booking-source labels the demo shows (Opera source codes — anything that
--    names a company or agency goes into the "replace" pairs, then re-run the
--    twins file and wait for the next sync)
select distinct x->>'source' as source
  from hotels t
  join briefings b on b.hotel_id = t.id
     and b.report_date = (select max(report_date) from briefings where hotel_id = t.id)
  cross join lateral jsonb_array_elements(coalesce(b.data->'sources_by_month', '[]'::jsonb)) x
 where t.pms_config ? 'demo_of'
 order by 1;
