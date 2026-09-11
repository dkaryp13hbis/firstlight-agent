-- 004: per-entity AI-narration toggle (user 2026-09-11: control token
-- spend per hotel/company/group). NULL = inherit; resolution is
-- hotel -> company -> group -> default ON. When off, briefings still
-- publish with deterministic fallback cards — zero Claude tokens.
alter table hotels        add column if not exists ai_enabled boolean;
alter table organizations add column if not exists ai_enabled boolean;
alter table groups        add column if not exists ai_enabled boolean;
