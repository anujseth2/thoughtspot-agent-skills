"""Resolve a Tableau consuming workbook's centralized-data references into a
TML-ready source spec.

Tableau workbooks reference centralized data three ways; the base convert-from-tableau
flow only handles a direct embedded connection. This module adds the other two:

  - class='sqlproxy'                          -> Published Data Source. The workbook's
    sqlproxy dbname is NOT the warehouse table (it's the published-DS contentUrl), so we
    resolve via the datasource's <repository-location> id -> download its .tdsx -> parse.
  - class='publishedConnection' + resourceId  -> Virtual Connection. Resolve via the
    Tableau virtualconnections REST API (clean qualifiedName + columns + dbClass; no
    mangling, and it also carries RLS policyCollection).

Pure functions only (stdlib). The CLI command in commands/tableau.py wires these to the
authenticated TableauClient (datasources / download_datasource / request).
"""
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Tableau DT_* (Virtual Connection content) -> ThoughtSpot TML data_type
DT_MAP = {
    "DT_INTEGER": "INT64", "DT_I8": "INT64",
    "DT_R8": "DOUBLE", "DT_REAL": "DOUBLE", "DT_FLOAT": "DOUBLE", "DT_NUMERIC": "DOUBLE", "DT_DECIMAL": "DOUBLE",
    "DT_WSTR": "VARCHAR", "DT_STR": "VARCHAR", "DT_STRING": "VARCHAR",
    "DT_BOOL": "BOOL",
    "DT_DATE": "DATE", "DT_DBDATE": "DATE", "DT_DBTIMESTAMP": "DATETIME", "DT_DBTIME": "TIME",
}
# Tableau .tds local-type (Published Data Source) -> ThoughtSpot TML data_type
LOCAL_TYPE_MAP = {
    "integer": "INT64", "real": "DOUBLE", "string": "VARCHAR",
    "boolean": "BOOL", "date": "DATE", "datetime": "DATETIME",
}


def read_workbook_xml(path):
    """Return the inner .twb XML text from a .twb or .twbx (zip)."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.endswith(".twb")), None)
            return z.read(name).decode("utf-8", "replace") if name else None
    return path.read_text()


def read_tds_xml(path):
    """Return the inner .tds XML text from a .tdsx (zip) or a plain .tds."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.endswith(".tds")), None)
            return z.read(name).decode("utf-8", "replace") if name else None
    return path.read_text()


def find_references(twb_text):
    """Find each centralized-data reference in a consuming workbook.

    Returns a list of dicts with kind in {virtual_connection, published_datasource}.
    """
    root = ET.fromstring(twb_text)
    dsroot = root.find("datasources")
    refs = []
    for ds in (dsroot.findall("datasource") if dsroot is not None else []):
        if ds.get("name") == "Parameters":
            continue
        caption = ds.get("caption")
        for nc in ds.iter("named-connection"):
            c = nc.find("connection")
            if c is not None and c.get("class") == "publishedConnection":
                refs.append({"kind": "virtual_connection", "caption": caption,
                             "resource_id": c.get("resourceId"), "resource_name": c.get("resourceName")})
        c0 = ds.find("connection")
        if c0 is not None and c0.get("class") == "sqlproxy":
            repo = ds.find("repository-location")
            refs.append({"kind": "published_datasource", "caption": caption,
                         "dbname": c0.get("dbname"),
                         "repo_id": repo.get("id") if repo is not None else None,
                         "repo_site": repo.get("site") if repo is not None else None})
    return refs


def parse_tds_structure(tds_text):
    """Parse a published datasource .tds into a source spec."""
    root = ET.fromstring(tds_text)
    db_class = next((c.get("class") for c in root.iter("connection")
                     if c.get("class") and c.get("class") != "federated"), None)
    server = next((c.get("server") for c in root.iter("connection") if c.get("server")), None)
    cols_by_parent = {}
    for rec in root.iter("metadata-record"):
        if rec.get("class") != "column":
            continue
        parent = (rec.findtext("parent-name") or "").strip("[]")
        name = (rec.findtext("local-name") or "").strip("[]")
        physical = rec.findtext("remote-name") or name
        ltype = (rec.findtext("local-type") or "").lower()
        cols_by_parent.setdefault(parent, []).append(
            {"name": name, "physical": physical, "tableau_type": ltype,
             "ts_type": LOCAL_TYPE_MAP.get(ltype, "VARCHAR")})
    tables, seen = [], set()
    for r in root.iter("relation"):
        if r.get("type") != "table":
            continue
        qualified = r.get("table")
        if not qualified or qualified in seen:
            continue
        seen.add(qualified)
        parts = [p.strip("[]") for p in qualified.split(".")]
        db = parts[0] if len(parts) == 3 else None
        schema = parts[1] if len(parts) == 3 else None
        tables.append({"database": db, "schema": schema, "qualified": qualified,
                       "columns": cols_by_parent.get(r.get("name"), [])})
    return {"db_class": db_class, "server": server, "tables": tables}


def parse_vc(detail_json, connections_json):
    """Parse a Virtual Connection's REST detail + connections into a source spec."""
    import json as _json
    vc = (detail_json or {}).get("virtualConnection", {})
    content = _json.loads(vc.get("content", "{}")) if vc.get("content") else {}
    cc = content.get("policyCollection", {})
    rls_policies = cc.get("policies", []) if isinstance(cc, dict) else []
    tables_in = content.get("revision", {}).get("revisableProperties", {}).get("tables", [])
    conn = ((connections_json or {}).get("virtualConnectionConnections", {}).get("connection") or [{}])[0]
    out = {"db_class": conn.get("dbClass"), "server": conn.get("server"),
           "rls_policy_count": len(rls_policies), "tables": []}
    for t in tables_in:
        rp = t.get("revisableProperties", {})
        pid = rp.get("physicalIdentifier", {})
        cols = []
        for col in rp.get("columns", []):
            crp = col.get("revisableProperties", {})
            cols.append({"name": crp.get("name"), "physical": crp.get("physicalColumnName"),
                         "tableau_type": crp.get("dataType"),
                         "ts_type": DT_MAP.get(crp.get("dataType"), "VARCHAR")})
        out["tables"].append({"database": pid.get("databaseName"), "schema": pid.get("schemaName"),
                              "qualified": pid.get("qualifiedName"), "columns": cols})
    return out


def caption_fallback(caption):
    """Layer-1 last resort: recover DB.SCHEMA.TABLE from a caption like 'Name (a.b.c) (x)'."""
    m = re.search(r"\(([A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\)", caption or "")
    return m.group(1) if m else None
