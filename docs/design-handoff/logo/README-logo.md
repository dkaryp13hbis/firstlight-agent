# FirstLight logo assets — for the PWA repo

The mobile app's header and icon live in the **firstlight-pwa** repo, not the
backend — these assets are ready to drop in there.

| File | Use |
|---|---|
| `firstlight-lockup.svg` | The PWA **header** brand ("lockup only · 24px mark" per the brand sheet). Mark left, wordmark First(ink)/Light(blue) right |
| `firstlight-icon.svg` | The **app icon** master (rounded square, gradient ring, corona rays, chart line + cyan dot, FL letterforms) |

## Export set needed from `firstlight-icon.svg`

- 1024×1024 — App Store
- 512×512 — PWA maskable (manifest `purpose: "maskable"`; keep the safe zone —
  the ring sits inside it)
- 180×180 + 120×120 — iOS touch icons
- 32×32 + 16×16 — favicon

## Rules (from the brand sheet)

- The web/BI header keeps the full lockup; the **app tile always drops the
  wordmark** (illegible at 40px) — icon = mark only.
- Letterforms use Manrope 800. If exporting from a tool without Manrope,
  install it first or convert the `<text>` elements to outlines — do not let
  it fall back to another font.
- Colours: ink `#16213B` · blue `#2E7CF7` · cyan `#38E1F0` · rays `#DFF7F9`.

## Note

The briefing HTML (backend-rendered) intentionally contains NO app header —
the PWA owns the header. The header redesign (12a spec: navy bar, frosted
pills, sync line) should be implemented in the PWA with this lockup.
