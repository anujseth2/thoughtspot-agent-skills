"""Unit tests for ts_cli.sisense.build_model — model assembly.

Pure functions, no live cluster. A tiny synthetic inventory exercises: most-connected
table = fact join orientation, default MANY_TO_ONE cardinality, the connection-name-only
invariant, the duplicate-column -> fact dedup (dimension-side duplicate dropped), and
calc-column formula translation (Migrated emitted, NEEDS REVIEW recorded but not emitted).
"""
from ts_cli.sisense.build_model import assemble


def _inv():
    return {
        "source": "Sales",
        "tables": [
            {"id": "Fact.csv", "name": "Fact", "columns": [
                {"id": "Amount", "name": "Amount", "data_type": "double", "calculated": False},
                {"id": "Dim1Id", "name": "Dim1Id", "data_type": "int64", "calculated": False},
                {"id": "Dim2Id", "name": "Dim2Id", "data_type": "int64", "calculated": False},
                {"id": "Cat", "name": "Cat", "data_type": "string", "calculated": False},
                {"id": "Margin", "name": "Margin", "data_type": "double",
                 "calculated": True, "expression": "sum([Amount])"},
                {"id": "Ranked", "name": "Ranked", "data_type": "double",
                 "calculated": True, "expression": "rank([Amount])"},
            ]},
            {"id": "Dim1.csv", "name": "Dim1", "columns": [
                {"id": "Dim1Id", "name": "Dim1Id", "data_type": "int64", "calculated": False},
                {"id": "Name", "name": "Name", "data_type": "string", "calculated": False},
                {"id": "Cat", "name": "Cat", "data_type": "string", "calculated": False},
            ]},
            {"id": "Dim2.csv", "name": "Dim2", "columns": [
                {"id": "Dim2Id", "name": "Dim2Id", "data_type": "int64", "calculated": False},
                {"id": "Label", "name": "Label", "data_type": "string", "calculated": False},
            ]},
        ],
        "relations": [
            {"endpoints": [{"table": "Dim1.csv", "column": "Dim1Id"},
                           {"table": "Fact.csv", "column": "Dim1Id"}], "cardinality": "UNKNOWN"},
            {"endpoints": [{"table": "Dim2.csv", "column": "Dim2Id"},
                           {"table": "Fact.csv", "column": "Dim2Id"}], "cardinality": "UNKNOWN"},
        ],
        "warnings": [],
    }


def _build():
    files, mapping = assemble(_inv(), {}, "MyConn", "db1", "sch1", "LEFT_OUTER", False)
    model = next(tml for fn, tml in files if fn.endswith(".model.tml"))["model"]
    tables = [tml for fn, tml in files if fn.endswith(".table.tml")]
    return files, mapping, model, tables


def test_files_emitted():
    files, _, _, tables = _build()
    assert len(tables) == 3                                # Fact + Dim1 + Dim2
    assert sum(1 for fn, _ in files if fn.endswith(".model.tml")) == 1


def test_join_orientation_and_cardinality():
    _, _, model, _ = _build()
    joins = {t["name"]: t.get("joins", []) for t in model["model_tables"]}
    # Fact is most-connected -> it is the source side of both joins.
    assert len(joins["Fact"]) == 2
    assert joins["Dim1"] == []
    assert joins["Dim2"] == []
    j = {jj["with"]: jj for jj in joins["Fact"]}
    assert j["Dim1"]["cardinality"] == "MANY_TO_ONE"      # UNKNOWN in file -> default
    assert j["Dim1"]["type"] == "LEFT_OUTER"
    assert j["Dim1"]["on"] == "[Fact::Dim1Id] = [Dim1::Dim1Id]"


def test_duplicate_column_bound_to_fact():
    _, _, model, _ = _build()
    cols = {c["name"]: c for c in model["columns"]}
    # "Cat" exists in both Fact (score 2) and Dim1 (score 1) -> single column, bound to Fact.
    cat = [c for c in model["columns"] if c["name"] == "Cat"]
    assert len(cat) == 1
    assert cat[0]["column_id"] == "Fact::Cat"             # dimension-side duplicate dropped


def test_connection_name_only_no_fqn():
    _, _, _, tables = _build()
    conn = tables[0]["table"]["connection"]
    assert conn == {"name": "MyConn"}                     # repo invariant: name only, never fqn
    assert "fqn" not in conn


def test_db_table_strips_csv():
    _, _, _, tables = _build()
    fact = next(t for t in tables if t["table"]["name"] == "Fact")
    assert fact["table"]["db_table"] == "Fact"           # .csv stripped, not lowered (flag off)


def test_calc_column_formula_migrated():
    _, mapping, model, _ = _build()
    fmap = {f["name"]: f["expr"] for f in model.get("formulas", [])}
    assert fmap["Margin"] == "sum([Amount])"
    rows = {m["name"]: m for m in mapping["measures"]}
    assert rows["Margin"]["status"] == "Migrated"


def test_calc_column_needs_review_not_emitted():
    _, mapping, model, _ = _build()
    emitted = {f["name"] for f in model.get("formulas", [])}
    rows = {m["name"]: m for m in mapping["measures"]}
    assert "Ranked" not in emitted                        # rank() -> NEEDS REVIEW
    assert rows["Ranked"]["status"] == "NEEDS REVIEW"
    assert rows["Ranked"]["ts_formula"] == ""


def test_spotter_enabled_by_default():
    _, _, model, _ = _build()
    assert model["properties"]["spotter_config"]["is_spotter_enabled"] is True
