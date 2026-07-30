# FirstLight logo — CANONICAL assets

The logo geometry is **final and canonical** — the SVG code in these two files
is copied verbatim from the brand source. **Never redraw, never edit
coordinates** (hard rule from the brand sheet; approximations are how logos
drift).

| File | Contains |
|---|---|
| `logo-9g-sunrise-code.html` | The 9g "Sunrise" mark: #1 ink-on-light · #2 white-on-dark · #3 **APP TILE** (white, thin gradient ring — this IS the app icon; the gradient-tile variant was removed per product owner) · #5 lockup composition |
| `lockup-firstlight-code.html` | Full lockups: A on light · B on dark (navy headers) · C on gradient |

## For the PWA repo

- **Header brand**: lockup **B** (white-on-dark, 26px mark, "Light" in cyan
  `#38E1F0` — blue fails contrast on navy)
- **App icon / tile**: variant **#3** — render at 1024, export the set:
  1024 (App Store) · 512 maskable (PWA manifest) · 180/120 (iOS) · 32/16
  (favicon). Below 20px the corona drops per the rules; at favicon sizes use
  the monogram without rays.
- Wordmark font is **Outfit 700** per the brand sheet — the brand may keep
  Outfit even though the app UI is Manrope; that is a brand-level choice
  already encoded in the canonical files. Load Outfit for the lockup only.

## Hard rules (from the source, do not violate)

- Geometry is FINAL — never edit path/rect coordinates
- Unique gradient ids per document instance (rename `g9g-*` / `fl-lock-*` per copy)
- Two-tone split is always First|Light — never colour other letters
- The dot: no glow, no halo, no shadow
- Corona: 8 rays ≤32px, 10 rays on tiles; opacity .16 light / .22 dark
- Minimum mark 16px; minimum lockup mark 20px (below that, wordmark alone)
