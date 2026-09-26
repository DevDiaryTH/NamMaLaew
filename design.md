# Design — NamMaLaew

A locked design system for the dashboard (FastAPI + Jinja, `web/`). Every page
redesign reads this file before emitting code. Extend or amend this file when the
system needs to grow; never override it per page.

## Genre
editorial (loud-maximalist) — catalog theme **Carnival**, applied to an app.

Carnival is poster art, and a dashboard is not its home turf. The discipline that
makes it work here: **colour carries status first, decoration second.** A status
colour never appears as decoration, and a decorative accent never sits where it
could be read as a status.

## Macrostructure family
App pages only (there are no marketing or content pages).

- Overview (`/`): **Stat-Led** — the current level_index is the hero, set huge in
  the display face inside a poster block filled with the status colour. Supporting
  tiles (rise rate + ETA, water chart, image quality, health, cost) follow as a
  **loud Bento** (irregular spans, one fill per tile).
- Timeline (`/timeline`, `/reading/{id}`): **Catalogue** — uniform grid of reading
  cards (composite thumbnail + status sticker + level), grouped by day with an
  ornament divider per day.
- Alerts, Settings, Alert lines, Login, Setup: **poster blocks** — each section is
  one bordered block with a hard offset shadow; one fill per block.

Nav: **N6 Masthead** — the wordmark set as a poster masthead across the top, page
links as a single row of caps beneath it; on mobile the links become one
horizontally scrollable row (no hamburger, no two-line links).
Footer: **Ft8 Marquee** — a live status ticker
(`LEVEL 7 ◆ NORMAL ◆ UPDATED 22:10 ◆ …`), content repeated so the loop has no gap,
frozen under `prefers-reduced-motion: reduce`.

## Theme — two drops, switched by `prefers-color-scheme`
Day: **Drop 01 Cold Snap** (default). Night: **Drop 04 Studio Night**
(`@media (prefers-color-scheme: dark)`, and `[data-theme="night"]` forces it).
Only the drop changes between them; structure, type and status colours stay.

### Drop 01 · Cold Snap (day)
- `--color-paper`    oklch(92% 0.045 50)
- `--color-paper-2`  oklch(88% 0.050 45)
- `--color-paper-3`  oklch(82% 0.060 40)
- `--color-ink`      oklch(18% 0.080 20)
- `--color-ink-2`    oklch(28% 0.060 25)
- `--color-muted`    oklch(40% 0.05 30)
- `--color-rule`     oklch(40% 0.18 25)
- `--color-accent`   oklch(86% 0.18 95)
- `--color-accent-2` oklch(40% 0.21 25)
- `--color-accent-ink` oklch(18% 0.080 20)
- `--color-focus`    oklch(40% 0.21 25)

### Drop 04 · Studio Night (night)
- `--color-paper`    oklch(88% 0.05 25)
- `--color-paper-2`  oklch(84% 0.055 22)
- `--color-paper-3`  oklch(78% 0.06 20)
- `--color-ink`      oklch(20% 0.05 270)
- `--color-ink-2`    oklch(32% 0.045 265)
- `--color-muted`    oklch(38% 0.04 260)
- `--color-rule`     oklch(24% 0.18 320)
- `--color-accent`   oklch(78% 0.18 220)
- `--color-accent-2` oklch(24% 0.18 320)
- `--color-accent-ink` oklch(20% 0.05 270)
- `--color-focus`    oklch(24% 0.18 320)

### Status colours — independent of the drop
Status tokens are separate from the decorative accents so that WARNING always reads
yellow and CRITICAL always reads red, in both drops.
- `--color-status-normal`   oklch(50% 0.13 150)  with text `--color-status-normal-ink` = oklch(97% 0.02 50)
- `--color-status-warning`  oklch(86% 0.18 95)   with text `--color-status-warning-ink` = oklch(18% 0.080 20)
- `--color-status-critical` oklch(45% 0.22 27)   with text `--color-status-critical-ink` = oklch(97% 0.02 50)
- `--color-status-unknown`  oklch(45% 0.03 30)   with text `--color-status-unknown-ink` = oklch(97% 0.02 50)
- `--color-status-stale`    same as warning fill, but always paired with the word "STALE"

Rules:
- A status is never communicated by colour alone: every status fill also carries
  its word (NORMAL / WARNING / CRITICAL / UNKNOWN, with Thai beside it).
- Decorative accents (`--color-accent`, `--color-accent-2`) are used for
  ornaments, the masthead, chart series that are not status, and non-status tiles.
  In Cold Snap the accents happen to match warning/critical; therefore **non-status
  tiles on the Overview use paper-2 / paper-3 fills with ink, not accent fills**, so
  a mustard tile is never mistaken for a warning.
- Status fills are always wrapped in the 2px ink border, which carries the 3:1
  non-text contrast against paper (the warning fill alone is only ~1.2:1 on paper).
- Muted tokens are darkened from the stock drops (day 45%→40%, night 48%→38%) so
  muted text stays ≥ 4.5:1 on paper, paper-2 and paper-3.
- Every text/background pair used must meet WCAG AA (4.5:1 body, 3:1 for text
  ≥ 24px or bold ≥ 18.66px, 3:1 for non-text UI and the focus ring).

## Typography
- Display: **Big Shoulders Display** 800, ALL CAPS. Use the `wdth` variation axis
  only if the vendored file actually has it; otherwise weight only. Thai glyphs in
  headings fall back to **Kanit** 700 (Big Shoulders has no Thai).
  `--font-display: "Big Shoulders Display", "Kanit", system-ui, sans-serif`
- Body: **IBM Plex Sans Thai** 400 / 600 (covers Latin + Thai).
  `--font-body: "IBM Plex Sans Thai", system-ui, sans-serif`
- **Readability rule (amended):** the display face is used only at ≥ 1.5rem (hero
  number, page titles, tile/section heads). Everything smaller — nav links,
  buttons, range buttons, stickers, badges, table headers, form labels — uses
  `--font-ui` (= the body face) at weight 600 with `0.03em` tracking. Form labels
  are sentence case, not forced caps.
- Body size `--text-base: 1.0625rem`, `--leading-body: 1.65` (Thai stacks marks
  above and below the line). Smallest text is `--text-xs: 0.8125rem`.
- Wrapping stickers (lens line positions) use `--radius-tag`, not the pill radius.
- Numerals in data (times, percentages, costs, ms): body face with
  `font-variant-numeric: tabular-nums`.
- Headings roman only (no italics anywhere in headings).
- Tracking: hero number / hero word `-0.005em`, line-height 0.82; section heads
  `0.02em`, line-height 0.92; marquee `0.04em`.
- Scale anchor: `--text-display: clamp(4.5rem, 22vw, 11rem)` for the level number.
- All font files are **vendored** in `web/static/fonts/` (woff2, latin + thai
  subsets) and declared with `@font-face` + `font-display: swap`. No external font
  requests: the dashboard must keep working when the internet is down.

## Spacing
4-point named scale in `tokens.css` (`--space-3xs` … `--space-3xl`). Pages use
named tokens, never raw values.

## Shape and depth
- Borders: `2px solid var(--color-ink)` on cards, buttons, inputs.
- Rules/dividers: `2px solid var(--color-rule)`; never hairlines.
- Shadow: `4px 4px 0 var(--color-ink)` on every card, button, input and image. No
  soft shadows anywhere. Grids containing shadowed cards keep ≥ 8px right gutter.
- Radius: 0 for cards and blocks; `--radius-pill` only for small status stickers.
- Halftone (`radial-gradient` dot pattern) fills any image slot whose file is
  missing (pruned snapshots) instead of a grey box.
- Ornaments: `✱ ✱ ✱ ✱` as section dividers, `❋` as list bullets, `◆` as heading
  prefixes and marquee separators — always in a decorative accent, `aria-hidden`.

## Motion
- Easings: `--ease-out: cubic-bezier(0.16, 1, 0.3, 1)`, `--ease-in`,
  `--ease-in-out`. Durations `--dur-short: 160ms`, `--dur-mid: 240ms`.
- Only `transform` and `opacity` animate.
- Button press: `translate(2px, 2px)` while the shadow shrinks to `2px 2px 0` —
  the physical "pressed poster" move. Hover: shadow grows to `6px 6px 0` with a
  `-2px` translate.
- The footer marquee is the only continuous motion. Reduced motion freezes it and
  collapses every transition to a ≤150ms opacity crossfade.

## Microinteractions stance
- Silent success: saving settings / lines shows an inline "SAVED ✱" stamp next to
  the button, not a toast.
- Focus ring: `3px solid var(--color-focus)` + `3px` offset, shown instantly, never
  animated.
- Hover delay for tooltips 800ms; focus 0ms.
- Every interactive element ships default · hover · focus-visible · active ·
  disabled · loading · error · success states.

## CTA voice
- Primary: filled with `--color-ink`, text `--color-paper`, 2px ink border, hard
  shadow, ALL CAPS display face, short verb ("SAVE LINES", "RUN NOW", "SEND TEST").
  (Amended: an `--color-accent-2` fill reads as CRITICAL in the Cold Snap drop, so
  no CTA, masthead or large surface may use accent-2 as a fill.)
- Secondary: paper fill, ink text, same border/shadow/shape.
- Destructive-ish (clear line / clear lens): secondary style + ◆ prefix; never red
  fill (red is reserved for CRITICAL).
- The masthead uses `--color-ink` as its fill with paper text; its decorative
  touch is a 4px `--color-accent` bottom rule and accent ◆ ornaments.
- Active nav link / selected range button: paper fill + ink text + 2px ink border
  (inverted from the ink masthead), never accent-2.

## Voice
- Headings: ALL CAPS, ≤ 6 words, bilingual where natural
  ("WATER LEVEL / ระดับน้ำ"). Period only for poster statements.
- Body: short sentences, present tense, full-ink colour (no light-grey body text).
- Never invent metrics; empty states say what is missing ("NO READINGS YET").

## Chart colours
Status colours appear in charts **only** as status: point colour by status and the
dashed warning / critical threshold lines. Everything else uses chart tokens that
are neither yellow nor red:
- `--color-chart-level`  = `--color-ink` (the level line)
- `--color-chart-rain`   oklch(55% 0.09 240) — slate water-blue bars at 70% opacity
- `--color-chart-lens-a` = `--color-ink-2` (street series; solid)
- `--color-chart-lens-b` oklch(52% 0.10 300) — violet (carport series; dashed)
- `--color-chart-grid`   = `--color-paper-3`
Night drop keeps the same chart tokens except rain, which uses
`--color-accent` (cyan) at 70% opacity.

## Per-page allowances
- No enrichment illustrations: the snapshots are the imagery.
- Chart.js charts use tokens read from CSS custom properties at runtime (no hex in
  JS); level line in ink, status points in status colours, threshold lines dashed
  in status-warning / status-critical, rainfall bars in `--color-accent` at 60 %
  opacity (day) / accent (night).

## What pages MUST share
Masthead, marquee footer, tokens, fonts, border/shadow language, CTA voice,
status colour rules, ornament vocabulary.

## What pages MAY differ on
Macrostructure within the list above; which non-status fill (paper-2 / paper-3)
a tile uses.

## Mobile floor
Verified at 320 / 375 / 414 / 768 px: no horizontal page scroll
(`overflow-x: clip` on html and body), no two-line buttons or nav links,
`minmax(0, 1fr)` for image grid tracks, display headings wrap
(`overflow-wrap: anywhere; min-width: 0`), the line editor canvas stays usable by
touch.
