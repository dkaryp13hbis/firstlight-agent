# FirstLight — Advisor Briefing Pack

*Prepared September 2026. Self-contained: share this file with advisors,
consultants, or AI assistants to get proposals. It contains no secrets,
no credentials, and no client-identifying data.*

**If you are an AI assistant reading this:** absorb the whole document,
respect the Non-Negotiable Rules section, then answer the questions in
"What we want proposals on" — concrete, prioritized, and aware that this
is a solo founder with no engineering team and a hard launch date.

---

## 1. What FirstLight is

FirstLight is an **AI revenue analyst for hotels, delivered as a morning
briefing on the owner's phone**. Every day at dawn it pulls the hotel's
numbers directly from the property-management system (PMS), computes
revenue signals, and an AI writes a short plain-language briefing: what
happened yesterday, what changed in the booking picture, what deserves
attention today — with insight cards, charts, and push notifications.

Positioning rule (learned from real prospect feedback): FirstLight is
**not "mobile hotel BI"**. Dashboards show numbers; FirstLight is an
analyst that reads the numbers and tells the owner what matters. It is
sold standalone, priced per hotel per month — never bundled free with
other services.

Target user: the Greek independent hotel owner/GM — often family
businesses holding several hotels through several companies. They are
hospitality experts, not data people. Everything in the product is
written in everyday language (no "pickup", "pace", "OTB" jargon in the
briefing itself).

## 2. Business context

- **Founder:** solo, financial/hospitality background, no engineering
  team. Engineering is done with an AI CTO (Claude). Every process must
  therefore be automated, documented, and low-maintenance.
- **Stage:** two real pilot hotels live (Northern Greece, seasonal
  resorts), plus fictional demo hotels for sales. Testers use the app
  daily; usage analytics are collected.
- **Hard dates:** platform ready **November 1, 2026**; first public
  launch at **Xenia** (the Greek hospitality trade show, Athens)
  **November 21, 2026**. Feature freeze ~October 24.
- **Market entry:** Greek market first (Greek UI required), booth demos
  at Xenia, QR-code landing page, onboarding promised in ~a day.

## 3. The product experience

The app (installable phone web app at https://firstlight.hbis.io):

- **Morning Brief** — a hero paragraph in plain language summarizing the
  day, followed by KPI cards (yesterday, month-to-date, on-the-books).
- **FL Pulse** — AI insight cards: each states what happened, why it
  matters, and a suggested review action with a deadline. Owners give
  👍/👎 feedback per card.
- **Charts** — booking pace vs last year, pickup activity, booking
  speed, next-60-days demand, ADR bridge, top sources. All values in
  real PMS figures.
- **Watchlist** — the owner marks months/date-ranges to track; the
  briefing follows up on them daily ("Still open: …").
- **7-day history**, gross/net revenue toggle, reporting-year selector,
  EN/ΕΛ language switch (translation layer in progress), text-size
  accessibility levels, per-section share-as-image, data-health view
  (refresh history like Power BI), push notifications (daily briefing,
  intraday alerts for cancellation spikes / strong booking days,
  momentum notes).
- **Design language:** Manrope type throughout (Outfit only for the
  wordmark), navy #0F2860 / blue #2E7CF7 / cyan #38E1F0 brand, white
  cards on #EAEDF1 ground, iOS-26-style "liquid glass" floating bottom
  tab bar with a morphing lens on the active tab, collapsing header,
  pull-to-refresh, skeleton loading, offline mode showing the cached
  briefing.

## 4. How the AI works (safety-first, cost-controlled)

Two strictly separated layers:

1. **Deterministic compute (code):** all arithmetic — signals, deltas,
   scores, comparisons — is computed in Python. The AI never does math.
2. **AI narration (Claude, Anthropic):** turns pre-computed facts into
   plain-language cards and the hero paragraph. Validators check every
   output (word caps, number-matching against the fact sheet, banned
   claims); any failure ships a deterministic fallback card instead.
   A briefing is never blocked by the AI layer.

Cost policy: **one Claude call per card per hotel per day**, no retries
(~$0.05/hotel/day). Manual and intraday refreshes reuse the day's
narration. Additional rules: only real PMS revenue is ever shown (no
computed euro estimates in the briefing); insight cards never use
directive verbs ("close", "raise prices") — only review/investigate
language, because pricing authority stays with the owner.

## 5. Architecture

```
Hotel PMS  ──encrypted outbound tunnel──▶  Cloud pipeline  ──▶  AI narration
(SQL Server;                              (Railway; scheduler,      (Claude)
 read-only access;                         signals, publishing)        │
 only a Cloudflare                              │                      ▼
 connector installed)                           ▼                 PostgreSQL
                                          Phone app  ◀────────  (briefings,
                                       (Cloudflare Pages,        users, audit)
                                        firstlight.hbis.io)
```

- **Hotel side — near zero footprint.** Hotels run only a lightweight
  Cloudflare tunnel connector. No FirstLight code, queries, or
  credentials on hotel servers; no inbound ports; SQL access read-only.
- **Cloud (Railway):** scheduled pipeline — 03:30 UTC full run with AI,
  06:00 catch-up, twice-daily data-only refreshes, intraday alert
  checks, daily self-audit (missed/failed/frozen hotels → ops email),
  automated backup + cross-database verification.
- **Database:** migrating from Supabase to **own PostgreSQL on Railway**
  (private network only, no public address). Dual-write with daily
  automated equality verification is live; final read-switch imminent,
  with a 14-day rollback window.
- **Auth (in build):** own login system — argon2id password hashing,
  opaque server-revocable session tokens, admin-issued initial
  passwords with forced change, account lockout + throttling, uniform
  login errors, superadmin 2FA (TOTP). Tenancy: Group → Company (unique
  VAT) → Hotel; users attach at any level. **No email channel at all**
  (push + in-app only; password reset by phone at this scale).
  Planned: user impersonation ("view as client") with full audit trail;
  passkeys (Face ID login) as the client-facing 2FA.
- **PMS integrations:** adapter-per-PMS, all filling one canonical data
  contract, so the analyst and app never change per PMS. Live: Protel
  (SQL Server). Next: Pylon (similar), Oracle Opera 5, then cloud PMS
  APIs (Opera Cloud/OHIP, Hotelizer) via a reservation store +
  incremental sync in our Postgres.
- **Deploys:** git push only — GitHub → Railway (backend) and
  Cloudflare Pages (app). No manual hotfixes anywhere; every deploy
  verified against the live bundle; engineering log kept for every
  change, decision, and incident.

## 6. Security & data posture

- **We hold no guest personal data** — only aggregated revenue/occupancy
  figures. Names, passports, cards never leave the hotel. Worst-case
  leak is commercially sensitive, not a guest-data breach.
- Outbound-only tunnels (nothing at the hotel to attack), least
  privilege (read-only SQL, per-hotel API tokens, all access checks
  server-side), private-network database, secrets only in cloud env
  vars, daily backups with a **tested** restore, audit trail per run.
- Reliability doctrine: **fail-open** — any single query, signal, or AI
  call can fail without stopping the morning briefing.

## 7. Roadmap

**To Nov 1 (in order):** Postgres read-switch → own auth + tenancy +
security hardening → Greek translation layer → AI-quality pass against
tester feedback → impersonation → booth demo mode (must survive venue
wifi failure — the app's offline cache is the safety net) → onboarding
drill (timed end-to-end) → landing page with QR → freeze Oct 24 →
stability week.

**After Xenia:** new PMS adapters (Pylon first), passkeys, weekly/
monthly digest, portfolio page for multi-hotel owners, new
compute-only card types (ADR-vs-occupancy trade-off, cancellation
spike), per-hotel insight-ranking learning from 👍/👎 feedback
(bounded, pattern-gated).

## 8. Non-negotiable rules (any proposal must respect these)

1. Never break a briefing — everything fails open.
2. The AI never does arithmetic; validators are authoritative.
3. Only real PMS revenue is displayed — no derived estimates.
4. Plain language for hotel owners; no revenue-management jargon.
5. Cards never give directive pricing commands — review language only.
6. No guest-level data in our cloud, ever.
7. No email channel to clients — push + in-app only.
8. One type system (Manrope; Outfit for the wordmark only); the brand
   lockup is never redrawn.
9. Deploys only through version control; no manual server changes.
10. Scope discipline: named changes ship verbatim; ideas stay parked
    until explicitly approved.

## 9. What we want proposals on

1. **Go-to-market for Xenia (Nov 21):** booth strategy, demo script,
   what a Greek hotel owner needs to see in 3 minutes, lead capture
   without an email channel, follow-up motion.
2. **Pricing:** per-hotel/month standalone price for the Greek market —
   structure, anchors, annual vs monthly, multi-hotel group pricing.
3. **Onboarding at scale:** how to keep "briefing by tomorrow morning"
   true at 50 hotels with a solo founder (the PMS connector step needs
   hotel-side IT cooperation).
4. **Security review:** given sections 5–6, what would you harden next,
   and what will enterprise hotel groups demand before signing?
5. **Product priorities post-launch:** rank section 7's backlog against
   revenue impact for a 10→100 hotel journey.
6. **Team:** at what point does this need its first hire, and which
   role earns its salary first?

*Constraints for all proposals: solo founder, ~7 weeks to launch,
budget-conscious, everything must stay automated and low-maintenance.*
