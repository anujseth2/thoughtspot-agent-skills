"""Dashboard/visual extraction from a Tableau workbook (open item #20).

Turns each `<dashboard>` into the `build_from_spec` dashboard shape (visuals with
mark + fields tagged by shelf/role/measure + date bucket tokens + a grid tile), so
`ts tableau parse` → `ts tableau build-liveboard` runs with no hand-assembled spec.
This is the codification of the previously agent-driven Tableau liveboard step (the
FedEx-harness `build_fedex_liveboard_*.py` method). Pure functions, no I/O.
"""
from __future__ import annotations

import re
from typing import Optional
import xml.etree.ElementTree as ET

from ts_cli.tableau.liveboard import leaf_name, role_for_shelf

# Tableau derivation → aggregate? (drives measure detection) and → date bucket keyword.
_AGG = {"Sum", "Avg", "Average", "Count", "Cnt", "CntD", "Min", "Max", "Median",
        "Stdev", "Stdevp", "Var", "Varp", "Attr"}
_TRUNC = {"Month-Trunc": "monthly", "Year-Trunc": "yearly", "Quarter-Trunc": "quarterly",
          "Week-Trunc": "weekly", "Day-Trunc": "daily", "Hour-Trunc": "hourly"}


def _instances(ws: ET.Element) -> dict:
    return {c.get("name"): c for c in ws.findall(".//datasource-dependencies/column-instance")}


def _shelf_refs(text: Optional[str]) -> list[str]:
    """Pull the `[inst]` keys out of a shelf's `([ds].[inst] * [ds].[inst] ...)` text."""
    return re.findall(r"\]\.(\[[^\]]+\])", text or "")


def _resolve(inst_key: str, ci: dict, captions: dict) -> Optional[dict]:
    c = ci.get(inst_key)
    if c is None:
        return None
    raw = c.get("column")                         # e.g. [Tailgating Events (copy)_NNN] or [sales]
    name = captions.get(raw) or leaf_name(raw)    # resolve calc-id → display caption
    if not name:
        return None
    deriv = c.get("derivation") or "None"
    bucket = _TRUNC.get(deriv)
    # measure iff aggregated, or a quantitative field that isn't a date bucket
    measure = (deriv in _AGG) or (c.get("type") == "quantitative" and not bucket)
    return {"name": name, "measure": measure, "bucket": bucket}


def worksheet_visual(name: str, ws: ET.Element, captions: dict) -> Optional[dict]:
    """One worksheet → a build_from_spec visual (mark + fields + bucket_tokens)."""
    mark_el = ws.find(".//mark")
    mark = (mark_el.get("class") if mark_el is not None else "") or "Automatic"
    cols_el = ws.find(".//table/cols")
    rows_el = ws.find(".//table/rows")
    ci = _instances(ws)

    fields: list[dict] = []
    bucket_tokens: dict[str, str] = {}
    seen: set[str] = set()

    def add(inst_key: str, shelf: str) -> None:
        f = _resolve(inst_key, ci, captions)
        if not f or f["name"] in seen:
            return
        seen.add(f["name"])
        role = role_for_shelf(shelf, f["measure"])
        fields.append({"name": f["name"], "measure": f["measure"], "role": role})
        if f["bucket"]:
            bucket_tokens[f["name"]] = f"[{f['name']}].{f['bucket']}"

    for k in _shelf_refs(cols_el.text if cols_el is not None else ""):
        add(k, "cols")
    for k in _shelf_refs(rows_el.text if rows_el is not None else ""):
        add(k, "rows")
    for enc in ws.findall(".//encodings/color"):
        m = re.search(r"\]\.(\[[^\]]+\])", enc.get("column") or "")
        if m:
            add(m.group(1), "color")

    if not fields:
        return None
    return {"title": name, "mark": mark.lower(), "fields": fields,
            "bucket_tokens": bucket_tokens}


def _zone_tile(z: ET.Element) -> Optional[dict]:
    """Tableau 0–100,000 zone coords → a 12-col × ~20-row ThoughtSpot grid tile."""
    try:
        x, y, w, h = (int(z.get("x")), int(z.get("y")), int(z.get("w")), int(z.get("h")))
    except (TypeError, ValueError):
        return None
    return {
        "x": max(0, min(11, round(x / 100000 * 12))),
        "y": max(0, round(y / 100000 * 20)),
        "width": max(2, min(12, round(w / 100000 * 12))),
        "height": max(3, round(h / 100000 * 20)),
    }


def extract_dashboards(root: ET.Element) -> list[dict]:
    """All `<dashboard>` elements → build_from_spec `dashboards[]`.

    Each viz zone (a zone carrying a worksheet `name`) becomes a visual; zones are
    deduped by worksheet name; layout/filter/legend/param/text zones are skipped
    (they have no `name`). Tiles come from the zone's grid coordinates.
    """
    ws_by_name = {w.get("name"): w for w in root.findall(".//worksheets/worksheet")}
    captions = {col.get("name"): col.get("caption")
                for col in root.findall(".//column") if col.get("caption")}
    dashboards: list[dict] = []
    for d in root.findall(".//dashboards/dashboard"):
        visuals: list[dict] = []
        seen: set[str] = set()
        for z in d.findall(".//zone"):
            wsname = z.get("name")
            if not wsname or wsname in seen or wsname not in ws_by_name:
                continue
            seen.add(wsname)
            v = worksheet_visual(wsname, ws_by_name[wsname], captions)
            if v:
                tile = _zone_tile(z)
                if tile:
                    v["tile"] = tile
                visuals.append(v)
        dashboards.append({"name": d.get("name") or "Dashboard", "visuals": visuals})
    return dashboards
