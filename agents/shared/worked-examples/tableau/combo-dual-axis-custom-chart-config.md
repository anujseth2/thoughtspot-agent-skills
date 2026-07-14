<!-- currency: tableau — 2026-07 (technique ported from the verified powerbi combo example; ThoughtSpot side verified live on ps-internal) -->
# Worked example — Tableau dual-axis combo (line + column) → `custom_chart_config`

## Source (Tableau)

A Tableau worksheet with a **dual axis**: `SUM([Sales])` as **bars** on the primary axis and
`SUM([Profit Ratio])` (a %) as a **line** on the secondary axis, by `[Order Date]` (month).
In the TWB this shows up as two `<pane>` marks with different `mark class` values
(`Bar` + `Line`) and a synchronized/secondary axis on the second measure.

## Why the naive migration decays

Emitting `ADVANCED_LINE_COLUMN` and encoding the line-vs-column split + secondary axis in
`chart.client_state_v2` `axisProperties` (`axisType: Y`, `isOpposite: true`) **looks right on
first import but does not survive a re-render/re-push** — ThoughtSpot re-derives the chart and
collapses every non-primary measure onto one shared secondary axis. The split you carried over
from Tableau is silently lost.

## Durable migration — `custom_chart_config`

The line/column assignment and the dual axis are durable only in `chart.custom_chart_config`
(verified live: the config survived a re-push; `client_state_v2` did not):

```yaml
chart:
  type: ADVANCED_LINE_COLUMN
  chart_columns:
  - {column_id: "Order Date"}
  - {column_id: "Total Sales"}          # bars
  - {column_id: "Profit Ratio"}         # line
  custom_chart_config:
  - key: basic
    dimensions:
    - {key: x-axis,        axes: [{type: FLAT,   column: "Order Date"}]}
    - {key: y-axis-column, axes: [{type: MERGED, columns: ["Total Sales"]}]}
    - {key: y-axis-line,   axes: [{type: MERGED, columns: ["Profit Ratio"]}]}
    - {key: trellis-by}
    mode: AXIS_DRIVEN
  display_mode: CHART_MODE
answer_columns:
- {column_id: "Profit Ratio", format: PERCENTAGE}   # per-column format lives here, not on the chart
```

- `y-axis-column` = the measure(s) drawn as clustered **columns**; `y-axis-line` = the
  measure(s) drawn as the **line**, on their own axis (this is the dual-axis effect).
- Both y-shelves use **`type: MERGED`** with a `columns:` list (not the plain-cartesian
  `type: FLAT`/`column:` form). `x-axis` stays `FLAT`.
- Map the Tableau assignment directly: the Bar-mark measure → `y-axis-column`, the Line-mark
  measure → `y-axis-line`. A stacked-column variant uses `ADVANCED_LINE_STACKED_COLUMN`.
- Requires the **Muze** charting library (Step 10-charts = M). On a Legacy-only cluster,
  fall back to two separate tiles (a COLUMN and a LINE) and flag the merged dual-axis as a
  migration gap — never put `custom_chart_config` on a Legacy type (import fails).

## Gotcha

Per-column display format (e.g. the ratio as a percent) belongs on `answer_columns[].format`,
not inside the chart block. Tab GUIDs regenerate on every TML import (tabs are keyed by name),
so a bookmarked `.../tab/<guid>` URL breaks after each re-push — don't rely on tab GUIDs.
