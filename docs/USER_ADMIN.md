# Users & access — runbook

**Audience:** HBIS engineering / operations. **Owner:** whoever changes the
superadmin portal (`/superadmin-control`) or the login code.
**Keep it current:** every change to the portal's Users screen, the login
flow, roles or the `users.py` CLI updates this file in the same commit
(standing rule, CLAUDE.md). Last verified against backend `f6cbc9b`+ and
PWA `5160440` — 2026-10-28.

---

## 1. How login works (read once)

- **One login system: ours.** Accounts live in our Railway Postgres
  (`users`, `sessions`, `memberships`). Supabase Auth was retired on
  2026-10-28 (`AUTH=own` on the web service; the app has no Supabase
  fallback). Code: `db/auth.py`, `db/passwords.py`, `api.py` (`/auth/*`,
  `/admin/users*`), app `web/src/lib/session.ts`, portal `web/src/portal/users.tsx`.
- **The email is only the login name.** Nothing is ever sent to it — there
  is no email channel (decision 2026-09-10). Passwords are handed over **by
  phone or in person**, never by email or chat.
- **Access = memberships.** A user can hold memberships at any level of
  **Group → Company → Hotel**; a membership grants every hotel below it,
  including hotels added later (view `hotel_access`).

  | Membership on | Gives | Use for |
  |---|---|---|
  | Group | every hotel of every company in the group + the group's **portfolio** entry (when ≥ 2 of its hotels) | owners of several companies (e.g. Tor Hotel Group) |
  | Company | every hotel of that company | an owner / GM of one legal entity |
  | One hotel | that hotel only | department staff, a single-hotel GM |

- **Roles.** `owner` or `viewer`. Both see the same screens today; `owner`
  is the role that will be allowed to edit the hotel's season dates in the
  app once the season release ships (currently uncommitted). HBIS staff can
  always edit.
- **HBIS platform admin** (`users.is_platform_admin`) opens the portal and
  the admin API. It does **not** add hotels to the admin's own app picker —
  admins see in the app only what their memberships give them (the portal
  sees everything).

## 2. Before you create a user

The hotel, its company and (if any) its group must already exist — see
[ONBOARDING.md](ONBOARDING.md) Step 2 for hotels and companies. The portal
pickers only list **active** hotels; demo twins (HBIS City / Boutique /
Grand) are inactive by design, so give demo access through the
**HBIS Hotels** group instead.

Collect per person (ONBOARDING.md intake 0.6–0.7): name, email (login
name), level (group / company / hotel), role, language (EN / ΕΛ).
One person = one account; never shared credentials (the demo account is
the only exception, §6).

## 3. Create a user — portal (preferred, audited)

1. Open **https://firstlight.hbis.io/superadmin-control** signed in with an
   HBIS platform-admin account → **Users**.
2. In **New user** fill:
   - **Email (login name — nothing is sent to it)** — required.
   - **Name**, **Language** (EN / ΕΛ).
   - **Password (blank = generate one)** — leave blank unless the person
     asked for a specific one. Rule: at least 10 characters, letters **and**
     numbers. Generated ones are 12 characters without look-alikes
     (no 0/O/1/l/I) so they can be read out by phone.
   - **Access level** → Group (all its hotels) / Company (its hotels) /
     One hotel, then pick it, then **Role** (Owner / Viewer).
   - **No forced change at first login** — leave OFF for real people (they
     must choose their own password at first sign-in). ON only for shared
     or demo accounts.
   - **HBIS platform admin** — HBIS staff only.
3. **Create user** → an amber box shows the password **once**
   ("read it out by phone, it is not shown again") with **Copy**. Hand it
   over by phone, then press **Done**. If it is lost: reset (§4).
4. The new row appears in the table with state *temporary password* until
   the first sign-in.

Every portal action is written to the **Audit log** section (`admin_audit`).

## 4. Day-to-day changes (click the user's row)

| Task | Where in the row detail | Effect |
|---|---|---|
| Add a hotel / company / group | **Access** → Access level + pick + Role → **+ Add access** | immediate (the app picks it up on the next hotel-list load) |
| Remove access | **Access** → **Remove** next to the membership | immediate; with no access left the app shows an empty picker ("No access yet") |
| Forgotten password | **Password** → New password (blank = generate) → **Reset password** | password shown once; **signs the user out of every device**; forced change unless *no forced change* is ticked |
| Leaver / suspend | **Account** → **Deactivate** | signs out everywhere, login refused; history kept |
| Return | **Account** → **Activate** | can sign in again with the existing password |
| Locked out | row shows *locked* / "locked until …" | lock lifts by itself after 15 minutes, or reset the password |

The detail also shows live sessions and failed-login count.

## 5. Hand-over to the user (what to tell them)

1. Open **https://firstlight.hbis.io**, sign in with the email + the
   password you read out → choose a new password (unless no forced change).
2. **iPhone:** Share → **Add to Home Screen**, then open FirstLight from the
   home screen — iOS only delivers notifications to the installed app.
   Android: install from the browser menu.
3. Tap the **bell** for each hotel they want notifications for (per hotel);
   Settings → Notifications chooses Morning / Alerts / Momentum.
4. Settings → **Language** sets the briefing narration language for that
   hotel from the next morning.
5. Settings → **Change password** any time (signs out their other devices).

## 6. Special accounts

- **demo@hbis.io** — the shared booth / prospect account. Viewer of the
  **HBIS Hotels** group (anonymized twins of the Tor hotels, refreshed
  automatically — ENGINEERING_LOG "DEMO ACCOUNT → TOR TWINS"). Created with
  *no forced change*. Rotate the password after each event (§4 reset, tick
  *no forced change*) and tell whoever uses it.
- **Platform admins** (dk@bi-automations.com, d.karypidis@hbis.io): password
  only today — a second factor (passkeys) is planned, see the log.
- **Client accounts** — Dinos (owner, Tor Hotel Group): leave his password
  as it is (user decision 2026-10-28).

## 7. CLI fallback (portal down, bulk work) — NOT audited

Runs inside the web container, so no key ever sits on a laptop. Prefer the
portal: CLI actions do **not** appear in the Audit log.

```
railway ssh -s web -- python scripts/users.py list
railway ssh -s web -- python scripts/users.py create --email a@b.gr --name Name --group <slug|uuid> --role owner [--lang el] [--password ...] [--keep-password] [--admin]
railway ssh -s web -- python scripts/users.py grant  --email a@b.gr (--group <slug> | --org <slug> | --hotel <uuid>) [--role viewer]
railway ssh -s web -- python scripts/users.py reset  --email a@b.gr [--password ...] [--keep-password]
railway ssh -s web -- python scripts/users.py deactivate --email a@b.gr
railway ssh -s web -- python scripts/users.py activate   --email a@b.gr
```

`create` prints the password once in YOUR terminal; it answers
"A user with this email already exists." when the account is there → use
`reset`. `--keep-password` = no forced change.

## 8. Security rules built in

- Passwords stored as scrypt hashes; session tokens stored only as sha256
  hashes (`fl_…` opaque tokens, 30-day sliding expiry).
- 10 wrong passwords → account locked 15 minutes; 30 attempts per network
  per 10 minutes → blocked (429).
- Changing or resetting a password signs the other devices out;
  deactivating signs out everywhere.
- The read-only database role cannot see password-hash columns.

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| App picker empty after sign-in | no membership | §4 add access |
| Portal Users: "The user store is not reachable (own-login SQL not applied, or the API is down)" | API down (check `/health`), or an old pre-2026-10-28 sign-in in that browser | open https://firstlight.hbis.io, sign in again, return to the portal |
| Portal: "Sign in first in the app" | no FirstLight session in this browser | sign in in the app first |
| No portfolio entry | fewer than 2 visible hotels of that group in the user's access | grant the group, or the missing hotel |
| "Too many attempts. Try again in 15 minutes." | lockout | wait, or reset (§4) |
| No notifications on iPhone | app not installed to the home screen, or bell off for that hotel | §5 steps 2–3 |

## 10. Verify after every new account

- [ ] Row shows the right **Access** (scope name + role) and state
      *temporary password* (or *active* with no forced change).
- [ ] The person signed in once and changed the password (state → active,
      *Last login* filled).
- [ ] They see exactly the expected hotels (+ portfolio when group).
- [ ] Bell on for the hotels they want; first morning push arrives.

## Change history

| Date | Change | Commit |
|---|---|---|
| 2026-10-28 | First version: own login only (`AUTH=own`), portal Users screen, demo account, CLI fallback | (this commit) |
