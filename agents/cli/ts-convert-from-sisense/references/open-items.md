# Open items — ts-convert-from-sisense

Unverified assumptions / follow-ups. Status vocabulary: `TO VERIFY | VERIFIED | KNOWN |
DEFERRED | WONT-FIX`. Each must reach VERIFIED (live or via MCP spec) or be explicitly
deferred before this ships live.

## #1 — Live end-to-end conversion on a real cluster — TO VERIFY (merge gate)

The full chain (`parse` → `build-model` → `ts tml lint` → `ts tml import` → `build-liveboard`)
has been exercised only against synthetic/sample bundles, not a live ThoughtSpot cluster with a
real warehouse connection. Before merge, run a captured Sisense bundle end-to-end on
ps-internal: confirm the Table + Model TMLs validate against a real connection, the model
creates clean (with the prune step removing any engine-rejected formula), and a spot-checked
measure returns faithful numbers (grouped sum equals the ungrouped total, so a `MANY_TO_ONE`
join does not fan out). This is the blocking item for shipping live.

## #2 — Shared liveboard emitter (`build_from_spec`) not yet on main — KNOWN / DEFERRED

`build-liveboard` computes the full `build_from_spec` spec + the extracted Sisense filter chips
today, but the final Answer/Liveboard **TML emission** is gated on the shared emitter
`ts_cli.tableau.liveboard.build_from_spec`, which is not yet on this branch. The injection code
is already in place: `ts_cli/commands/sisense.py` guards the import and, when the emitter is
absent, emits `{"status": "spec_only", "spec": ..., "filter_chips": ...}` to stdout and exits 0
(a valid partial). Everything behind the `if build_from_spec is not None` branch — Answer/
Liveboard TML file-writes + chip injection into `liveboard.filters` — auto-activates the moment
the emitter merges to main. No skill or CLI change is required when it lands; re-run
`build-liveboard` and the TML is written. Deferred until the emitter branch merges.

## #3 — Live Sisense REST fetch not built — DEFERRED

The converter consumes an **offline bundle JSON** (`{dashboard, widgets, datamodel}`) already on
disk; there is no command that pulls a dashboard, its widgets, and the datamodel directly from a
Sisense server's REST API. A user must assemble the bundle from a Sisense export first. A future
`ts sisense fetch` (following the `.claude/rules/ts-cli.md` "new command" flow: MCP-spec the
Sisense REST endpoints, verify live, then add the command) would close this. Deferred — the
offline path is the intended v1 surface.

## #4 — JAQL formula subset breadth — TO VERIFY on real dashboards

The deterministic `FUNCTION_MAP` / `AGG_MAP` in `ts_cli/sisense/functions.py` covers the common
subset observed in the sample bundles. Against a broader set of real customer dashboards, some
functions currently emitted as Migrated may still be rejected by the ThoughtSpot engine (e.g.
`median`, `stddev`/`variance` argument shapes), and some now marked NEEDS REVIEW may turn out to
have a safe deterministic port. Confirm against real JAQL on the first few live migrations; the
runtime prune step (Step 2) is the safety net until then.

## #5 — Numeric-range / date-bucket filter-chip fidelity — TO VERIFY live

The dashboard-filter → Liveboard-chip mapping (member→`IN`, exclude→`NOT_IN`, numeric range→
`GE`/`GT`/`LE`/`LT`/`BW_INC`/`BW`/`EQ`; date `level`→`HOURLY…YEARLY`) is derived from the
standalone converter and the worked examples, not yet round-tripped through a live Liveboard
import. Confirm the chip operators and bucket tokens render and filter correctly on a real
Liveboard once the emitter (#2) lands.
