"""Sisense -> ThoughtSpot Table TML + naming/type/cardinality helpers.

Ported from the standalone converter (map/model.py). Pure functions, no I/O.
Repo invariant: a table TML connection block carries the connection display NAME
only, never a GUID (.claude/rules/ts-cli.md, the "connection_name" convention).

Physical-name conventions (from the standalone converter):
  db_table       = SourceTable.id with a trailing ".csv" stripped (optionally lowered)
  db_column_name = column display name with spaces -> underscores
  logical name   = the Sisense display name (kept, may contain spaces)
"""
from __future__ import annotations

import re

# Normalized type token (from parsing._to_datatype) -> TML data_type enum.
_TML_TYPE = {
    "int64": "INT64",
    "double": "DOUBLE",
    "bool": "BOOL",
    "string": "VARCHAR",
    "date": "DATE",
    "datetime": "DATE_TIME",
    "unknown": "VARCHAR",
}

# Sisense v2 `relations` does NOT export cardinality (defaults to UNKNOWN), so this
# maps a known value through and defaults the rest to MANY_TO_ONE (fact -> dimension).
_CARD = {
    "many_to_one": "MANY_TO_ONE", "one_to_one": "ONE_TO_ONE",
    "one_to_many": "ONE_TO_MANY", "many_to_many": "MANY_TO_MANY",
}


def _slug(name) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(name)).strip("-").lower() or "obj"


def _clean(table_id) -> str:
    """Table id -> logical table name: strip a trailing '.csv' (Sisense CSV datasets)."""
    s = str(table_id)
    return s[:-4] if s.lower().endswith(".csv") else s


def _dbname(name) -> str:
    """Display name -> a warehouse-safe physical column name (spaces -> underscores)."""
    return str(name).replace(" ", "_")


def _is_id(name) -> bool:
    return str(name).strip().lower().endswith("id")


def _tml_type(data_type) -> str:
    return _TML_TYPE.get((data_type or "").strip().lower(), "VARCHAR")


def _col_role(col: dict) -> tuple:
    """Infer (column_type, aggregation) for a parsed Sisense column.

    An explicit `role` wins; otherwise numeric IDs are attributes (not measures),
    other numerics are SUM measures, and everything else is an attribute."""
    role = (col.get("role") or "").strip().upper() if col.get("role") else None
    if role in ("ATTRIBUTE", "MEASURE"):
        return role, ("SUM" if role == "MEASURE" else None)
    if _is_id(col.get("name", "")):
        return "ATTRIBUTE", None
    if (col.get("data_type") or "").strip().lower() in ("int64", "double"):
        return "MEASURE", "SUM"
    return "ATTRIBUTE", None


def _cardinality(rel: dict) -> str:
    """Relation cardinality -> TML cardinality; default MANY_TO_ONE (fact -> dimension)."""
    return _CARD.get((rel.get("cardinality") or "").strip().lower(), "MANY_TO_ONE")


def build_table_tml(table: dict, connection_name: str, db: str, schema: str,
                    warnings: list, lower_db_table: bool = False) -> tuple:
    """Build a Table TML. Returns (tml_dict, dropped_column_display_names).

    Logical column/table names stay the Sisense display names (the model references
    those); db_table / db_column_name are the warehouse-safe physical names. The
    connection block carries name only (never fqn) per the repo invariant.
    """
    cols = []
    for c in table.get("columns", []):
        ctype, agg = _col_role(c)
        props = {"column_type": ctype}
        if agg:
            props["aggregation"] = agg
        cols.append({
            "name": c["name"],
            "db_column_name": _dbname(c["name"]),
            "properties": props,
            "db_column_properties": {"data_type": _tml_type(c.get("data_type"))},
        })
    name = _clean(table.get("id") or table.get("name"))
    db_table = name.lower() if lower_db_table else name
    tbl = {
        "name": name,
        "db": db,
        "schema": schema,
        "db_table": db_table,
        "connection": {"name": connection_name},
        "columns": cols,
    }
    obj = {"obj_id": f"{_slug(name)}-sisense", "table": tbl}
    return obj, []
