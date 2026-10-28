# Onboarding — cloud PMS (API)

**Audience:** HBIS engineering, and whoever talks to a prospect on a cloud
PMS. **Part of:** [ONBOARDING.md](ONBOARDING.md) Step 2.
**Keep it current:** the first cloud adapter that ships turns the
"planned" parts below into the real steps — update this file in that commit.
Last verified 2026-10-28.

> **Status: not live.** No cloud adapter exists yet — the registry
> (`db/adapters/base.py`) has `protel_mssql` and `opera_oracle` only;
> `hotelizer_api` is listed as planned. A hotel whose PMS is only reachable
> by API **cannot go live today**. Steps 1–3 below (qualify, access,
> discovery) can start now; Steps 4–6 need the shared sync engine and that
> PMS's adapter to be built first.

## How it differs from on-premises

| | On-premises ([runbook](ONBOARDING_ONPREM.md)) | Cloud / API (this page) |
|---|---|---|
| Connection | Cloudflare tunnel into the PMS database | the vendor's HTTPS API, with credentials the hotel grants |
| Where the data lives | stays in the PMS; we run bounded queries each refresh | **we pull and keep a copy** of reservation rows in our Postgres (no guest data) |
| First run | immediate — the queries read history directly | after a **2-year backfill** (needed for "same time last year" and last year's final) |
| Each refresh | the query pack runs against the PMS | an incremental sync (changes since the last run), then the same query pack runs on our copy |
| Hotel IT effort | install cloudflared + one read-only login | usually none — the hotel approves the integration in the vendor's marketplace / admin screen |
| Lead time | ~1 hour | vendor partner approval (days to weeks) + our build for a new PMS |

Why the copy: an API returns reservations, not the aggregates our signals
need. Decided 2026-08-18 (ENGINEERING_LOG "PMS integrations roadmap"): the
APIs give book date, cancellation date and last-modified, so after a
2-year backfill "same time last year" reconstructs exactly, and each
refresh only fetches what changed. The analyst, the cards and the app never
know which PMS produced the data (same HotelDataSnapshot contract).

## PMSs on the radar

| PMS | Access model (what we know) | Our state |
|---|---|---|
| **Opera Cloud** | Oracle Hospitality Integration Platform (OHIP): partner registration, OAuth, the app subscribed per property. Partner onboarding has a long lead time — start early. | not started |
| **Hotelizer** | REST, lighter than OHIP, same pull model | planned in the registry, not started |
| **Mews** | Connector API: one **ClientToken** per integration (issued by Mews after a certification run) + one **AccessToken** per property (the hotel grants it). | **prior art in Hotel Bi**: `hbis-hotel-bi/connectors/mews` (Power Query) is in Mews certification — 48 h+ run, then the form. Known quirk: availability capped at 100 days per call. FirstLight would need its own integration (or an agreed shared one). |
| Others (WebHotelier, …) | ask at qualification | — |

## Step 1 — Qualify (at the stand or on the first call)

Collect, in addition to [ONBOARDING.md](ONBOARDING.md) Step 0:

| # | Question | Why |
|---|---|---|
| A.1 | PMS name, edition, region / data centre | picks the adapter and the API base URL |
| A.2 | Number of properties on it; the property id(s) | one hotel row per property |
| A.3 | Who administers the PMS account (name, phone) | they approve the integration |
| A.4 | Does the PMS have a marketplace / integrations page, or do they get API keys from the vendor? | the access model (Step 2) |
| A.5 | How many years of history are in the PMS (at least 2 needed) | backfill |
| A.6 | Any channel manager / booking engine that writes into the PMS | source attribution |

What to say (copy rules of the Xenia plan apply — no promised dates or
results in public): "FirstLight works today with on-premises Protel and
Opera. For <PMS> we'll check the connection with your PMS team and come
back with a date." Never promise "tomorrow morning" for a cloud PMS until
its adapter is live.

## Step 2 — Get API access (days to weeks, mostly waiting)

1. **Partner side (once per PMS, HBIS):** register as an integration
   partner where the vendor requires it (OHIP, Mews certification);
   record the integration name, the sandbox credentials and the
   certification steps in this file.
2. **Property side (per hotel):** the hotel's PMS admin approves our
   integration for their property, or creates API credentials, and sends us
   the property id + credentials **by phone or the vendor's own flow** —
   never by email or chat.
3. Credentials are stored only in `hotels.pms_config` (the read-only
   database role cannot see that column) — never in the repo, a laptop file
   or a ticket.

## Step 3 — Discovery (≈ 2 days per new PMS, sandbox credentials)

Answer each question in writing in this file before building:

- **Cancellations:** does a cancelled reservation keep its stay dates and
  rate? How is the cancellation date exposed?
- **Modifications:** do changes and rebooks appear as a new record or an
  update? Is there a reliable last-modified filter for incremental sync?
- **Revenue:** room revenue vs packages and extras; gross vs net of VAT and
  city tax (Gross / Net toggle in the app); currency.
- **Nights and rooms:** room types, out-of-order and dummy rooms (the
  sellable count still comes from intake 0.3).
- **Sources:** channel / source / travel agent / rate code fields — what
  FL Pulse's source attribution can use.
- **History:** how far back the API serves (≥ 2 years needed).
- **Limits:** rate limits, page sizes, per-call date ranges (Mews: 100 days
  for availability), timezone of dates.

## Step 4 — Build (engineering; first cloud PMS only — then per PMS)

Planned shape (to be settled with the first PMS; nothing below exists yet):

1. **Reservation store** in our Postgres: one row per reservation-night or
   per reservation, holding stay dates, book date, cancellation date,
   status, room type, nights, room revenue (gross + net), source, rate code,
   last-modified, and a hashed reservation id. **No guest data at all** —
   no names, contacts, documents or card data (non-negotiable rule).
2. **Sync engine** (shared): a 2-year backfill job and an incremental job
   (changes since the stored cursor), with its state in its own table, not
   in `pms_config`.
3. **Adapter** `db/adapters/<pms>_api/`: maps the vendor's records to the
   reservation row, and `fetch_snapshot` runs the same query pack (Q1–Q19)
   over our copy.
4. **Fail-open** like everything else: a failed sync must never block the
   morning briefing — publish from the last good copy and show its age.

Rough sizing (estimate, refine after discovery): the shared store + sync
engine is a one-time build of one to two weeks; each further PMS is the
mapping + adapter, a few days. Planned after Xenia.

## Step 5 — Backfill and reconcile (per hotel)

Run the backfill, then compare with the PMS's own reports before any user
sees it: yesterday's room nights and revenue, this month's booked revenue,
last year's final per month (Opera was validated to the euro against Hotel
Bi — same bar here).

## Step 6 — The `pms_config` block and go-live

Proposed shape (settle with the first adapter):
```jsonc
{
  "fetch_mode": "api",
  "api": {
    "base_url": "<vendor API URL>",
    "property_id": "<A.2>",
    "auth": { "type": "<oauth2 | token>", "...": "<credentials from Step 2>" }
  },
  "hotel_type": "city",
  "language": "en"
}
```
`pms_type` = `<pms>_api`. Then [ONBOARDING.md](ONBOARDING.md) Steps 3–7
as for any hotel (Step 6 expects **Path** to read the API path instead of
tunnel).

## Open decisions

- Which cloud PMS first — decide from the Xenia leads (count of prospects
  per PMS).
- Store grain: per reservation-night (simplest queries) or per reservation
  (smaller) — at the first build.

## Change history

| Date | Change | Commit |
|---|---|---|
| 2026-10-28 | First version: status (not live), on-premises vs API, PMS radar (Opera Cloud / Hotelizer / Mews prior art), qualify → access → discovery → build → backfill → go-live, proposed `pms_config` | (this commit) |
