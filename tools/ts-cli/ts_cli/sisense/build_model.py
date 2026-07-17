"""Sisense -> ThoughtSpot Model TML assembly (joins, columns, calc-column formulas).

Ported from the standalone converter (map/model.py). Pure functions, no I/O. The
top-level entry point is assemble(): parsed inventory -> (files, mapping), mirroring
the Power BI converter's assemble. build_model_tml builds the Model TML itself.

Join orientation follows the standalone converter: the most-connected table is the
fact (the source side of every join it participates in); cardinality is read from the
relation (default MANY_TO_ONE).

Model columns preserve the standalone converter's dedup behaviour: exactly ONE column
per display name, bound to the MOST-CONNECTED table. When a key column (e.g. "Category
ID") appears in both the fact and a dimension, only the fact's copy survives as a model
column — the dimension-side duplicate is dropped. This drops the dimension-side column
rather than disambiguating it; it is preserved here intentionally (the model path
mirrors the source's behaviour, warts included).

Serialization is NOT done here (assemble returns dicts); the command module serializes
with the shared ts_cli.tml_common.dump_tml_yaml. Formula-name transforms reuse the
shared ts_cli.formula_common helpers (never re-implemented per platform).
"""
from __future__ import annotations

from collections import Counter

from ts_cli.formula_common import (add_formula_prefix, expr_is_aggregated,
                                    fix_double_aggregation, resolve_name_collisions)
from ts_cli.sisense.functions import translate_jaql
from ts_cli.sisense.tables import _cardinality, _clean, _col_role, _dbname, _slug, build_table_tml


def _connectivity(relations: list) -> Counter:
    """table_id -> number of relation endpoints it participates in (the fact is highest)."""
    part: Counter = Counter()
    for rel in relations:
        for ep in rel.get("endpoints", []) or []:
            if ep.get("table"):
                part[ep["table"]] += 1
    return part


def _build_joins(relations: list, part: Counter, table_ids: set, join_type: str) -> tuple:
    """Relations -> ({src_table_id: [join,...]} keyed by the fact/source side, rel status
    rows). The most-connected endpoint is the source (fact); cardinality via _cardinality."""
    joins_by_src: dict = {}
    rel_rows: list = []
    for rel in relations:
        eps = rel.get("endpoints") or []
        if len(eps) < 2:
            rel_rows.append({"name": "?", "status": "NEEDS REVIEW",
                             "note": "relation has fewer than two endpoints"})
            continue
        a, b = eps[0], eps[1]
        if a.get("table") not in table_ids or b.get("table") not in table_ids:
            rel_rows.append({"name": f"{a.get('table')}->{b.get('table')}",
                             "status": "NEEDS REVIEW",
                             "note": "relation references an unknown table"})
            continue
        src, dst = (a, b) if part[a["table"]] >= part[b["table"]] else (b, a)
        s_name, d_name = _clean(src["table"]), _clean(dst["table"])
        nm = f"{s_name}_to_{d_name}"
        card = _cardinality(rel)
        joins_by_src.setdefault(src["table"], []).append({
            "with": d_name,
            "on": f"[{s_name}::{src['column']}] = [{d_name}::{dst['column']}]",
            "type": join_type,
            "cardinality": card,
        })
        rel_rows.append({"name": nm, "status": "Migrated", "note": f"{join_type}, {card}"})
    return joins_by_src, rel_rows


def _build_model_columns(tables: list, part: Counter) -> list:
    """One model column per display name, bound to the most-connected table (see module
    docstring: the dimension-side duplicate is dropped — preserved from the source)."""
    best: dict = {}   # display name -> (connectedness score, table, column)
    for t in tables:
        score = part[t["id"]]
        for c in t.get("columns", []):
            cur = best.get(c["name"])
            if cur is None or score > cur[0]:
                best[c["name"]] = (score, t, c)
    mcols = []
    for _name, (_score, t, c) in best.items():
        ctype, agg = _col_role(c)
        props = {"column_type": ctype}
        if agg:
            props["aggregation"] = agg
        mcols.append({"name": c["name"], "column_id": f"{_clean(t['id'])}::{c['name']}",
                      "properties": props})
    return mcols


def _build_formulas(tables: list, columns: list) -> tuple:
    """Calculated columns (isCustom + expression) -> model formulas[] via translate_jaql.

    Sibling refs [Name] -> [formula_Name] id-refs (add_formula_prefix); a wrapped
    aggregation of an already-aggregated sibling is collapsed (fix_double_aggregation).
    Returns (formulas, formula_columns, measure_status_rows)."""
    formulas: list = []
    measure_rows: list = []
    for t in tables:
        for c in t.get("columns", []):
            if not (c.get("calculated") and c.get("expression")):
                continue
            expr, status, note = translate_jaql(c["expression"])
            measure_rows.append({"name": c["name"], "original": c["expression"],
                                 "ts_formula": expr or "", "status": status, "note": note})
            if expr:
                formulas.append({"id": f"formula_{c['name']}", "name": c["name"], "expr": expr})
    if not formulas:
        return [], [], measure_rows

    formula_names = {f["name"] for f in formulas}
    for f in formulas:
        f["expr"] = add_formula_prefix(f["expr"], formula_names, set())
    fexprs = {f["name"]: f["expr"] for f in formulas}
    for f in formulas:
        f["expr"] = fix_double_aggregation(f["expr"], fexprs)

    formula_columns = []
    for f in formulas:
        ctype = "MEASURE" if expr_is_aggregated(f["expr"]) else "ATTRIBUTE"
        formula_columns.append({"name": f["name"], "formula_id": f["id"],
                                "properties": {"column_type": ctype}})
    return formulas, formula_columns, measure_rows


def build_model_tml(inv: dict, model_name: str, join_type: str, part: Counter,
                    overrides: dict, warnings: list) -> tuple:
    """Return (model_tml_dict, measure_status_rows, rel_status_rows)."""
    tables = inv.get("tables", [])
    relations = inv.get("relations", [])
    table_ids = {t["id"] for t in tables}

    joins_by_src, rel_rows = _build_joins(relations, part, table_ids, join_type)
    model_tables = []
    for t in tables:
        entry = {"name": _clean(t["id"])}
        if t["id"] in joins_by_src:
            entry["joins"] = joins_by_src[t["id"]]
        model_tables.append(entry)

    columns = _build_model_columns(tables, part)
    formulas, formula_columns, measure_rows = _build_formulas(tables, columns)
    columns = columns + formula_columns
    # Resolve collisions between physical columns and calc-column formulas (formula wins).
    columns, formulas, _rename = resolve_name_collisions(columns, formulas, [])

    model = {
        "obj_id": f"{_slug(model_name)}-sisense",
        "model": {"name": model_name, "model_tables": model_tables, "columns": columns},
    }
    if formulas:
        model["model"]["formulas"] = formulas
    # Enable Spotter (NL search): a TML-imported model defaults it off, so the ai/answer
    # APIs return "No answer found" until set true. Default on; override spotter_enabled.
    props = {"spotter_config": {"is_spotter_enabled": overrides.get("spotter_enabled", True)}}
    props.update(overrides.get("model_properties") or {})
    model["model"]["properties"] = props
    return model, measure_rows, rel_rows


def assemble(inv: dict, overrides: dict, connection: str, db: str, schema: str,
             join_type: str = "LEFT_OUTER", lower_db_table: bool = False,
             model_name: str | None = None) -> tuple:
    """Parsed inventory -> (files, mapping). Pure (no I/O): emits a Table TML per source
    table + one Model TML, and returns files = [(filename, tml_dict), ...] plus the
    mapping.json status dict. The connection block carries name only (never fqn)."""
    overrides = overrides or {}
    warnings = list(inv.get("warnings", []))
    m_name = (model_name or overrides.get("model_name") or inv.get("source")
              or "Converted Model")

    conn = overrides.get("connection") or {}
    conn_name = conn.get("name") or connection
    dbn, sch = conn.get("db") or db, conn.get("schema") or schema

    tables = inv.get("tables", [])
    relations = inv.get("relations", [])
    part = _connectivity(relations)

    files: list = []
    table_rows: list = []
    for t in tables:
        tml, _dropped = build_table_tml(t, conn_name, dbn, sch, warnings, lower_db_table)
        name = _clean(t["id"])
        files.append((f"{_slug(name)}.table.tml", tml))
        table_rows.append({"name": name, "status": "Migrated",
                           "note": f"db_table '{tml['table']['db_table']}'; verify"})

    model_tml, measure_rows, rel_rows = build_model_tml(
        inv, m_name, join_type, part, overrides, warnings)
    files.append((f"{_slug(m_name)}.model.tml", model_tml))

    mapping = {"model_name": m_name, "tables": table_rows, "relationships": rel_rows,
               "measures": measure_rows, "warnings": warnings}
    return files, mapping
