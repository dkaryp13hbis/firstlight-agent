# FirstLight Mobile App — Placeholder Plan

> Status: **placeholder / not started** — created 2026-07-26.
> Decision pending. Do not begin implementation until the pilot phase is stable.

## Context

FirstLight is currently delivered as a server-rendered briefing page (PWA) hosted on
Railway, with web push (VAPID) for morning notifications. Users are hotel GMs and
revenue managers on iOS and Android phones.

## Options considered

| Option | Effort | Reuses current UI | iOS push quality | App store presence |
|---|---|---|---|---|
| 1. Keep PWA only | None | Yes | Weak (requires "Add to Home Screen", iOS 16.4+) | No |
| 2. **Capacitor wrapper** (recommended) | Low (1–2 weeks) | Yes — wraps existing web page | Native (APNs) | Yes |
| 3. Flutter rebuild | High (2–3 months) | No — full UI rewrite in Dart | Native | Yes |
| 4. React Native rebuild | High | No | Native | Yes |

**Recommendation: Option 2 (Capacitor) now; revisit Flutter (Option 3) only if we
later need offline-first mode, native charts, or app-specific UX the web view can't do.**

Why not Flutter first:
- The briefing UI already exists and works; Flutter means building and maintaining a
  second frontend in Dart alongside the web one.
- Our users' core loop is "receive push → open → read" — a wrapper covers this fully.
- Capacitor lets us ship to both stores in weeks, then swap the inside for Flutter
  later without changing the store listing.

## Action steps

### Phase 0 — Prerequisites (before any app work)
- [ ] Pilot hotels stable on the cloud (Railway + Supabase) architecture.
- [ ] Decide branding: app name ("FirstLight"), icon, splash screen.
- [ ] Register **Apple Developer account** ($99/year, needs a legal entity or individual; approval can take days).
- [ ] Register **Google Play developer account** ($25 one-time).
- [ ] Confirm the briefing page is fully mobile-responsive and works logged-in via a stable per-hotel URL/token.

### Phase 1 — Capacitor wrapper (target: 1–2 weeks of work)
- [ ] Create a minimal Capacitor project (`npm init @capacitor/app`) pointing at the hosted briefing URL (remote URL mode) or bundling a thin shell that loads it.
- [ ] Add native push: Capacitor Push Notifications plugin → Firebase Cloud Messaging (Android) + APNs (iOS).
- [ ] Backend change: extend the existing push module (`briefing/cloud_push.py`) to send via FCM/APNs **in addition to** web push — store device tokens per hotel/user in Supabase alongside existing VAPID subscriptions.
- [ ] Handle deep links: tapping the morning push opens today's briefing directly.
- [ ] Offline/error state: friendly "no connection" screen instead of a white WebView.
- [ ] App icons + splash screens (one source image, generated per platform).

### Phase 2 — Store submission
- [ ] Android: internal testing track on Google Play → invite pilot hotel GMs → promote to production.
- [ ] iOS: TestFlight build → invite pilot GMs → App Store review.
  - Note: Apple can reject "just a website in a wrapper" (guideline 4.2). Native push + app-specific niceties (biometric unlock, deep links) usually satisfy this. Have answers ready.
- [ ] Privacy policy page (required by both stores) — host on the marketing site.
- [ ] Store listing assets: screenshots (per device size), description, keywords.

### Phase 3 — Post-launch
- [ ] Monitor push delivery rates (native vs web push) per hotel.
- [ ] Collect GM feedback: is a native rebuild (Flutter) justified? Triggers would be:
      offline access to past briefings, native interactive charts, per-user settings UI.
- [ ] If yes → scope Flutter migration as a separate project; the store listing and
      push infrastructure carry over.

## Open questions
- [ ] Auth model for the app: per-hotel magic link? PIN? (Currently URL/token-based.)
- [ ] One app for all hotels vs. white-label per hotel group? (One app strongly preferred — white-label multiplies store submissions.)
- [ ] Who owns the Apple/Google developer accounts — BI Automations or the client hotels?
