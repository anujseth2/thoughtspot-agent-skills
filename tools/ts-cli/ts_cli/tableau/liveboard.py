"""Deterministic Answer + tabbed-Liveboard emission for the Tableau converter.

Pure functions (no I/O) behind `ts tableau build-liveboard`. The role-aware axis layout,
the overrides capture-and-replay, and the tab assembly are ported from the verified
standalone Power BI converter's `generate_tml.py` (`_answer_tml` / `_answer_tml_explicit`
/ `_liveboard_tml` / `chart_type_for` / `_CHART_NEEDS`) — the ThoughtSpot-side emission is
platform-neutral, so only the *input* adapter differs: Power BI feeds PBIR roles
(Category/Series/Rows/Columns/Values); Tableau feeds worksheet shelves (Columns/Rows/Color),
which `role_for_shelf` maps to the same canonical roles.

This replaces the LLM-executed prose templates in SKILL.md Step 10 with deterministic,
tested Python (the repo's "agentic → deterministic" codification angle). It emits the base
answer/liveboard TML; presentation polish that the skill still layers on (KPI sparkline
`client_state_v2`, Step 10.5 themes, Step 10f parameter chips) can be supplied per-visual
via the overrides mechanism (`client_state_v2` / `viz_style` / `custom_chart_config`).
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Tableau mark class → ThoughtSpot chart type. Unknown marks default to GRID_TABLE.
_MARK_TO_CHART = {
    "bar": "BAR", "line": "LINE", "area": "AREA",
    "circle": "SCATTER", "point": "SCATTER", "shape": "SCATTER",
    "square": "BAR", "pie": "PIE", "text": "GRID_TABLE", "gantt": "BAR",
    "automatic": "BAR",
}
# Marks / zone types that are not data visuals.
_NON_VISUAL = {"", "filter", "legend", "color", "paramctrl", "bitmap", "web",
               "extension", "flipboard", "flipboard-nav"}

# Minimum measure count a chart type needs to render (flag, never silently downgrade).
CHART_NEEDS = {"LINE_COLUMN": 2, "LINE_STACKED_COLUMN": 2, "SCATTER": 2,
               "ADVANCED_LINE_COLUMN": 2, "ADVANCED_BUBBLE": 2}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "viz"


def leaf_name(field: Optional[str]) -> str:
    """Strip a qualified Tableau field ref (`[Datasource].[Field]` / `T::Col`) to its leaf."""
    if not field:
        return ""
    f = field.strip().strip("[]")
    f = f.split("::")[-1]
    f = f.split("].[")[-1].strip("[]")
    return f.strip()


def chart_type_for_mark(mark_class: Optional[str]) -> tuple[Optional[str], str, str]:
    """Tableau mark class → (ts_chart, status, note). None ts_chart => skip (filter/legend)."""
    mk = (mark_class or "").strip().lower()
    if mk in _NON_VISUAL:
        return None, "Skipped", f"{mark_class!r} is not a data visual"
    if mk in _MARK_TO_CHART:
        ct = _MARK_TO_CHART[mk]
        if mk == "text":
            return ct, "Migrated", "crosstab → GRID_TABLE (use PIVOT_TABLE for a rows×cols matrix)"
        return ct, "Migrated", ""
    return "GRID_TABLE", "Approximated", f"no direct mapping for mark {mark_class!r}; defaulted to GRID_TABLE"


def role_for_shelf(shelf: Optional[str], is_measure: bool) -> str:
    """Map a Tableau shelf to the canonical role `build_answer` understands.

    Columns shelf → Category (x); Rows shelf (dimension) → Rows (pivot rows); Color/Detail
    → Series (color split); a measure → Values (measures always land on y regardless). This
    is the Tableau twin of Power BI's PBIR roles.
    """
    s = (shelf or "").strip().lower()
    if is_measure:
        return "Values"
    if s in ("color", "colour", "detail", "legend"):
        return "Series"
    if s in ("rows", "row"):
        return "Rows"
    if s in ("columns", "column", "cols"):
        return "Category"
    return "Category"


def auto_name(cols: list[str], measure_names: set) -> Optional[str]:
    """'<measures> by <attributes>' — how Tableau/Power BI auto-title a viz. None if it can't."""
    measures = [c for c in cols if c in measure_names]
    attrs = [c for c in cols if c not in measure_names]
    if measures and attrs:
        return f"{', '.join(measures)} by {' and '.join(attrs)}"
    return None


_CARTESIAN = ("COLUMN", "BAR", "LINE", "AREA", "STACKED_COLUMN", "STACKED_BAR",
              "LINE_COLUMN", "LINE_STACKED_COLUMN")


def _first(*groups: list) -> list:
    """First non-empty group, else []. Collapses the `(a or b or c)` fallback chains."""
    for g in groups:
        if g:
            return g
    return []


def _role_buckets(xs: list[str], role_of: dict) -> dict:
    """Split non-measure columns by canonical role → {cat, ser, rows, cols}."""
    def by_role(*names):
        return [c for c in xs if role_of.get(c) in names]
    return {"cat": by_role("Category", "Axis", "X"), "ser": by_role("Series", "Legend", "Group"),
            "rows": by_role("Rows"), "cols": by_role("Columns")}


def _xy_color(x: list, y: list, color: list) -> dict:
    ax = {"x": x, "y": y}
    if color:
        ax["color"] = color
    return ax


def _axis_configs(chart_type: str, cols: list[str], xs: list[str], ys: list[str],
                  role_of: dict) -> dict:
    """Role-aware axis_configs for one chart (ported from _answer_tml's axis branch)."""
    b = _role_buckets(xs, role_of)
    if chart_type == "KPI":
        return {"y": ys or cols}
    if chart_type == "PIE" and len(cols) >= 2:
        return {"x": _first(b["cat"], b["rows"], xs, [cols[0]])[:1], "y": _first(ys, [cols[-1]])[:1]}
    if chart_type == "PIVOT_TABLE" and ys:
        xax = _first(b["rows"], b["cat"], xs[:1], [cols[0]])[:1]
        return _xy_color(xax, ys, _first(b["cols"], b["ser"], [c for c in xs if c not in xax]))
    if chart_type in _CARTESIAN and len(cols) >= 2:
        xax = _first(b["cat"], b["rows"], xs, [cols[0]])[:1]
        return _xy_color(xax, ys or [cols[-1]],
                         _first(b["ser"], b["cols"], [c for c in xs if c not in xax])[:1])
    return {}


def build_answer(name: str, obj_key: str, model_name: str, model_fqn: Optional[str],
                 cols: list[str], chart_type: str, measure_names: set,
                 roles: Optional[list[str]] = None,
                 bucket_tokens: Optional[dict] = None) -> dict:
    """Role-aware Answer TML (ported from generate_tml._answer_tml).

    Measures go on y; non-measures are placed by role — Category/Axis/X → x,
    Series/Legend/Group → color, Rows/Columns → the pivot axes. A PIVOT_TABLE without
    `axis_configs` renders blank, so rows→x / values→y / columns→color is emitted explicitly.
    `bucket_tokens` overrides a column's search token (e.g. a monthly date bucket).
    """
    roles = roles or [""] * len(cols)
    bucket_tokens = bucket_tokens or {}
    role_of = {c: r for c, r in zip(cols, roles)}
    ys = [c for c in cols if c in measure_names]
    xs = [c for c in cols if c not in measure_names]
    search = " ".join(bucket_tokens.get(c, f"[{c}]") for c in cols)

    chart: dict[str, Any] = {"type": chart_type,
                             "chart_columns": [{"column_id": c} for c in cols]}
    ax = _axis_configs(chart_type, cols, xs, ys, role_of)
    if ax:
        chart["axis_configs"] = [ax]

    tables_ref = {"name": model_name}
    if model_fqn:
        tables_ref = {"id": model_name, "name": model_name, "fqn": model_fqn}
    return {
        "obj_id": f"{slugify(obj_key)}-tab",
        "answer": {
            "name": name,
            "display_mode": "CHART_MODE",
            "tables": [tables_ref],
            "search_query": search,
            "answer_columns": [{"name": c} for c in cols],
            "table": {"table_columns": [{"column_id": c} for c in cols],
                      "ordered_column_ids": list(cols)},
            "chart": chart,
        },
    }


def build_answer_explicit(name: str, obj_key: str, model_name: str,
                          model_fqn: Optional[str], ov: dict) -> dict:
    """Answer emitted verbatim from an override (ported from _answer_tml_explicit).

    Capture-and-replay of manual UI polish for visuals the auto-builder can't express:
    `ov['columns']` (ordered column ids), `ov['search']`, `ov['ts_chart']`, optional
    `ov['axis']`, and the round-trip-safe presentation blobs `formats` (per-column format),
    `client_state_v2`, `custom_chart_config` (the AUTHORITATIVE combo/dual-axis config —
    persists where client_state_v2 decays), `custom_visual_props`, `viz_style`.
    """
    cols = list(ov["columns"])
    fmts = ov.get("formats") or {}
    chart: dict[str, Any] = {"type": ov.get("ts_chart", "GRID_TABLE"),
                             "chart_columns": [{"column_id": c} for c in cols]}
    if ov.get("axis"):
        chart["axis_configs"] = [ov["axis"]]
    if ov.get("client_state_v2"):
        chart["client_state_v2"] = ov["client_state_v2"]
    for k in ("custom_chart_config", "custom_visual_props", "viz_style"):
        if ov.get(k) is not None:
            chart[k] = ov[k]
    tables_ref = {"name": model_name}
    if model_fqn:
        tables_ref = {"id": model_name, "name": model_name, "fqn": model_fqn}
    return {
        "obj_id": f"{slugify(obj_key)}-tab",
        "answer": {
            "name": name,
            "display_mode": "CHART_MODE",
            "tables": [tables_ref],
            "search_query": ov["search"],
            "answer_columns": [dict({"name": c}, **({"format": fmts[c]} if c in fmts else {}))
                               for c in cols],
            "table": {"table_columns": [{"column_id": c} for c in cols],
                      "ordered_column_ids": cols},
            "chart": chart,
        },
    }


def build_liveboard(name: str, tabs: list) -> dict:
    """One liveboard, one tab per Tableau dashboard (ported from _liveboard_tml).

    `tabs` = [(tab_name, [ {"answer": <answer payload>, "tile": {x,y,width,height}|None}, ... ]), ...].
    A tile carries the grid placement from Step 9c's container-tree layout; when absent it
    falls back to a two-per-row 6×8 grid. Empty tabs are dropped (they won't render).
    """
    viz, tab_layout, n = [], [], 0
    for tab_name, items in tabs:
        items = [it for it in items if it]
        if not items:
            continue
        tiles = []
        for j, it in enumerate(items):
            n += 1
            vid = f"Viz_{n}"
            viz.append({"id": vid, "answer": it["answer"]})
            tile = it.get("tile") or {"x": (j % 2) * 6, "y": (j // 2) * 8, "width": 6, "height": 8}
            tiles.append({"visualization_id": vid, **tile})
        tab_layout.append({"name": tab_name, "tiles": tiles})
    return {"obj_id": f"{slugify(name)}-tab",
            "liveboard": {"name": name, "visualizations": viz,
                          "layout": {"tabs": tab_layout}}}


def _collect_fields(vis: dict) -> tuple[list[str], list[str]]:
    """Dedupe a visual's fields → (cols, roles), each field tagged by explicit role or shelf."""
    cols, roles = [], []
    for f in vis.get("fields", []):
        col = f.get("name")
        if not col or col in cols:
            continue
        role = f.get("role") or role_for_shelf(f.get("shelf"), bool(f.get("measure")))
        cols.append(col)
        roles.append(role)
    return cols, roles


def _note_join(note: str, msg: str) -> str:
    return (note + "; " + msg) if note else msg


def _chart_needs(ct: str) -> int:
    if ct in CHART_NEEDS:
        return CHART_NEEDS[ct]
    return 0 if ct in ("GRID_TABLE", "PIVOT_TABLE") else 1


def _override_result(vis: dict, page: str, title: str, model_name: str,
                     model_fqn: Optional[str], ov: dict) -> tuple:
    a_obj = build_answer_explicit(vis.get("title") or title, title, model_name, model_fqn, ov)
    row = {"page": page, "visual": title, "ts_chart": ov.get("ts_chart", "?"),
           "status": ov.get("status", "Migrated"), "note": ov.get("note", "explicit override")}
    return a_obj, {"answer": a_obj["answer"], "tile": vis.get("tile")}, row


def _visual_to_result(vis: dict, page: str, vi: int, model_name: str,
                      model_fqn: Optional[str], measure_names: set) -> tuple:
    """One visual → (answer_obj|None, tile_item|None, visual_row). Pure decision logic
    extracted from build_from_spec so both stay under the complexity cap."""
    title = vis.get("title") or f"{page} - visual {vi + 1}"
    ov = vis.get("override")
    if ov and ov.get("search") and ov.get("columns"):
        return _override_result(vis, page, title, model_name, model_fqn, ov)

    ct, status, note = chart_type_for_mark(vis.get("mark"))
    if ct is None:
        return None, None, {"page": page, "visual": title, "ts_chart": "(skipped)",
                            "status": status, "note": note}

    cols, roles = _collect_fields(vis)
    if not cols:
        return None, None, {"page": page, "visual": title, "ts_chart": ct,
                            "status": "NEEDS REVIEW", "note": _note_join(note, "no fields on the visual")}

    n_meas = sum(1 for c in cols if c in measure_names)
    need = _chart_needs(ct)
    if n_meas < need:
        status = "NEEDS REVIEW"
        note = _note_join(note, f"{ct} needs {need} measure(s) but {n_meas} present; "
                                "flagged, not downgraded")

    name = vis.get("title") or auto_name(cols, measure_names) or title
    a_obj = build_answer(name, title, model_name, model_fqn, cols, ct,
                         measure_names, roles, vis.get("bucket_tokens") or {})
    row = {"page": page, "visual": title, "ts_chart": ct, "status": status, "note": note}
    return a_obj, {"answer": a_obj["answer"], "tile": vis.get("tile")}, row


def build_from_spec(spec: dict) -> dict:
    """Orchestrate a dashboard spec → answers + one tabbed liveboard + a per-visual report.

    `spec`:
      {
        "report_name": "...", "model_name": "...", "model_fqn": <optional>,
        "measure_names": ["Total Sales", ...],
        "dashboards": [
          {"name": "Overview", "tooltip": false, "visuals": [
             {"title": <optional>, "mark": "bar",
              "fields": [{"name": "Region", "shelf": "columns", "measure": false},
                         {"name": "Total Sales", "measure": true},
                         {"name": "Segment", "role": "Series"}],       # role wins over shelf
              "bucket_tokens": {"Month(Order Date)": "[Order Date].MONTHLY"},  # optional
              "tile": {"x":0,"y":0,"width":6,"height":8},              # optional (Step 9c)
              "override": {...}}                                        # optional explicit spec
          ]},
        ],
        "extra_visuals": [ {"page": "Overview", "name": "...", "search": "...",
                            "columns": [...], "ts_chart": "KPI"} ]      # optional added tiles
      }

    Returns {"answers": [...], "liveboard": {...}|None, "visual_rows": [...], "page_rows": [...]}
    — visual_rows/page_rows drive the Step 12 migration report.
    """
    model_name = spec["model_name"]
    model_fqn = spec.get("model_fqn")
    measure_names = set(spec.get("measure_names") or [])
    report_name = spec.get("report_name") or model_name
    extra = spec.get("extra_visuals") or []

    answers, tabs, visual_rows, page_rows = [], [], [], []

    for dash in spec.get("dashboards", []):
        page = dash.get("name") or "Dashboard"
        if dash.get("tooltip"):
            page_rows.append({"name": page, "status": "NEEDS REVIEW",
                              "note": "tooltip/hover overlay — no ThoughtSpot tab equivalent"})
            continue
        d_answers, items, d_rows = _dashboard_items(dash, page, model_name, model_fqn,
                                                    measure_names, extra)
        answers.extend(d_answers)
        visual_rows.extend(d_rows)
        tabs.append((page, items))
        page_rows.append({"name": page, "status": "Migrated" if items else "NEEDS REVIEW"})

    liveboard = build_liveboard(report_name, tabs) if any(it for _, it in tabs) else None
    return {"answers": answers, "liveboard": liveboard,
            "visual_rows": visual_rows, "page_rows": page_rows}


def _dashboard_items(dash: dict, page: str, model_name: str, model_fqn: Optional[str],
                     measure_names: set, extra: list) -> tuple:
    """One dashboard → (answers, tile_items, visual_rows) — visuals then override extra tiles."""
    answers, items, rows = [], [], []
    for vi, vis in enumerate(dash.get("visuals", [])):
        a_obj, item, row = _visual_to_result(vis, page, vi, model_name, model_fqn, measure_names)
        if a_obj:
            answers.append(a_obj)
        if item:
            items.append(item)
        rows.append(row)
    for ev in extra:
        if ev.get("page") == page and ev.get("search") and ev.get("columns"):
            nm = ev.get("name") or f"{page} - added"
            a_obj = build_answer_explicit(nm, f"{page}-extra-{len(items)}",
                                          model_name, model_fqn, ev)
            answers.append(a_obj)
            items.append({"answer": a_obj["answer"], "tile": ev.get("tile")})
            rows.append({"page": page, "visual": nm, "ts_chart": ev.get("ts_chart", "?"),
                         "status": "Migrated", "note": ev.get("note", "added tile")})
    return answers, items, rows
