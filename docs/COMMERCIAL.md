# FirstLight — Product & Commercial Policy

Living document for the commercial side of FirstLight: positioning, pricing,
bundles, and market policies. Technical decisions live in
[ENGINEERING_LOG.md](ENGINEERING_LOG.md); this file is where pricing and
go-to-market decisions get discussed and locked.

Status legend: **DECIDED** = locked, sell against it. **PROPOSED** = working
assumption, open for discussion. **OPEN** = not yet discussed to conclusion.

---

## 1. What FirstLight is (customer-facing definition)

FirstLight is a daily AI morning briefing for hotel leadership. Every morning
it reads the hotel's PMS, computes the revenue signals that matter (pickup,
pace vs last year, lead time, occupancy outlook), and delivers a short,
narrated briefing — insight cards plus a hero paragraph — to the GM's inbox
and phone before the workday starts.

It is a **snapshot product**: the answer to "what do I need to know this
morning?", not a place to spend an hour exploring data.

## 2. Market position — relationship to Hotel BI

- **Hotel BI** (Power BI, ~€330/mo + VAT, 64 active clients) is the **depth
  layer**: full drill-down analytics for people who work in the data.
- **FirstLight** is the **habit layer**: 2 minutes every morning, zero
  training, lands on its own.
- The **bridge** is the share/drill-down button: any visual in a FirstLight
  briefing can be shared with a colleague and opened in Hotel BI for full
  drill-down. Every drill-down click by a FirstLight-only customer is a
  qualified Hotel BI lead.

Consequences:

1. The products are sold **together and separately** — neither replaces the
   other, and the sales copy must never imply FirstLight is "Hotel BI lite".
2. FirstLight standalone must be priced clearly below Hotel BI so no Hotel BI
   client reads it as a downgrade path.
3. The existing 64-client base is the primary launch market: zero acquisition
   cost, data connections already trusted.

## 3. Pricing

### 3.1 FirstLight price — DECIDED

**€99 per hotel per month, + VAT. No user limit.**

- Flat per property — **not** priced by hotel size / room count.
- "No user limit" means: unlimited briefing recipients and app users per
  hotel — GM, owner, revenue manager, F&B, whoever the hotel wants. The unit
  we sell is the *hotel* (one PMS connection → one briefing per day), never
  seats.

Why flat, not per-size:

- Hotel BI trained the base on flat per-property pricing; mixing models
  invites every client to reopen the €330 conversation.
- The value unit is the briefing (one per hotel per day), not the room.
  COGS is flat per hotel (~€2–3/mo AI + infra share).
- €99 flat closes over email; per-room pricing needs a call, a room-count
  declaration, and enforcement.
- Unlimited users is a deliberate differentiator: BI tools charge per seat,
  FirstLight spreads through the hotel for free — every extra recipient
  deepens the habit and the lock-in.

### 3.2 Price architecture around it — PROPOSED

| Offer | Price (+VAT) | Notes |
|---|---|---|
| FirstLight add-on (existing Hotel BI client) | €99/mo | The launch motion. One-line upsell to the 64-client base. |
| Bundle: Hotel BI + FirstLight (new clients) | €399/mo | vs €429 separate — "save €30/mo". Becomes the default new-client offer. |
| FirstLight standalone | €99/mo | Same price as the add-on (one public price, no confusion). Its drill-down/share ceiling is the Hotel BI upsell funnel. |
| Portfolio / group | −20–30% from 2nd property | The only size-based lever we use. Triggered by multi-property owners, expected by management companies. |

- OPEN: launch promo for the existing base (e.g. €69/mo for the first 3
  months, or 2 months free on annual) to maximize attach rate fast.
- OPEN: annual prepay discount (e.g. 10 months for 12).

### 3.3 Revenue math (reference)

- Existing base: 64 clients × €99 at an attach rate of 50–60% ≈
  **€3.2–3.8k extra MRR (€38–45k/yr)** from an email campaign + sample
  briefing. This dwarfs standalone sales in year 1.
- COGS per hotel ≈ €2–3/mo AI (hard-capped: Claude runs once per hotel per
  day, single-shot narration) + infra share → **>95% gross margin** at €99.

## 4. Product policies

| Policy | Status | Position |
|---|---|---|
| User limit | DECIDED | None. Unlimited recipients/users per hotel. |
| Billing unit | DECIDED | Per hotel (one PMS connection = one hotel). |
| AI usage | DECIDED (eng) | One AI generation per hotel per day (03:30 UTC run); manual/data refreshes reuse the day's insights. This is also the fair-use answer if anyone asks "can we refresh 50×/day": data yes, AI narration no. |
| Reliability promise | DECIDED (eng) | A briefing is always published if data is valid — narration failures degrade to fallback cards, never block. Customer-facing wording: "your briefing arrives every morning". |
| Hotel-side footprint | DECIDED (eng) | Customer hardware runs only a Cloudflare tunnel — no code, queries, or keys on site. Sales point for IT-sensitive clients. |
| Trial | OPEN | Suggestion: 14 days free, real data, no card — the product sells itself after ~10 mornings. |
| Cancellation | OPEN | Suggestion: monthly rolling, cancel anytime — confidence signal, and churn risk is low for a daily-habit product. |
| Data ownership | OPEN | Suggestion: hotel's PMS data stays the hotel's; we store computed briefings + aggregates. Needs a written line in the contract. |
| PMS coverage | OPEN | Today: Protel (SQL Server). Roadmap: Opera, Fidelio, Hotelizer, Pylon. Do we charge for a new-PMS integration or eat it as market expansion? |

## 5. Selling points (for all copy/decks)

1. **"Your revenue analyst emails you every morning."** Anchor to a person's
   time, not to dashboards or reports.
2. **Zero training, zero setup on site.** GM opens an email/notification;
   IT installs one tunnel.
3. **Unlimited users.** The whole leadership team reads it — no seat math.
4. **Numbers you can trust.** All figures are computed deterministically from
   the PMS; the AI only narrates — it cannot invent a number.
5. **One tap to depth.** Share any visual with a colleague; drill down in
   Hotel BI when you need the full picture.

## 6. Open questions queue

- [ ] Launch promo for the 64-client base — shape and deadline.
- [ ] Annual prepay discount — yes/no and rate.
- [ ] Trial policy (14 days, real data?).
- [ ] Cancellation terms wording.
- [ ] Data-ownership clause for the contract.
- [ ] New-PMS integration: paid or free?
- [ ] Price review trigger: revisit €99 once FirstLight has ~20 standalone
  (non-Hotel-BI) customers — that's the signal it carries its own weight.

---

*Update this file whenever a commercial decision is made, and mirror durable
decisions into the ENGINEERING_LOG decision log with a date.*
