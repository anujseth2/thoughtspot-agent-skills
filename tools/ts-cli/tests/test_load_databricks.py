"""Unit tests for the pure SQL-builders behind `ts load databricks` (no live connection)."""
from ts_cli.commands.load import (
    dbx_type, build_dbx_create_sql, build_dbx_insert_sql, _sql_literal,
)


def test_dbx_type_mapping():
    assert dbx_type("VARCHAR(64)") == "STRING"
    assert dbx_type("CHAR") == "STRING"
    assert dbx_type("DATE") == "DATE"
    assert dbx_type("TIMESTAMP") == "TIMESTAMP"
    assert dbx_type("FLOAT") == "DOUBLE"
    assert dbx_type("NUMBER(38,2)") == "DOUBLE"   # has scale → DOUBLE
    assert dbx_type("NUMBER(38,0)") == "BIGINT"   # no scale → BIGINT
    assert dbx_type("INT") == "BIGINT"
    assert dbx_type("BOOLEAN") == "BOOLEAN"
    assert dbx_type("") == "STRING"


def test_create_sql_backticks_and_column_mapping():
    cols = [{"name": "Order Date", "type": "DATE"}, {"name": "Sales", "type": "NUMBER(38,2)"}]
    sql = build_dbx_create_sql("`c`.`s`.`orders_demo`", cols)
    assert "`Order Date` DATE" in sql          # space-containing name preserved 1:1
    assert "`Sales` DOUBLE" in sql
    assert "delta.columnMapping.mode" in sql   # column mapping enabled (else Delta rejects spaces)
    assert sql.startswith("CREATE TABLE IF NOT EXISTS `c`.`s`.`orders_demo`")


def test_sql_literal_quoting_by_type():
    assert _sql_literal("2024-01-02", "DATE") == "DATE'2024-01-02'"
    assert _sql_literal("123.45", "DOUBLE") == "123.45"
    assert _sql_literal("42", "BIGINT") == "42"
    assert _sql_literal("O'Brien", "STRING") == "'O''Brien'"   # single-quote escaped
    assert _sql_literal("", "STRING") == "NULL"
    assert _sql_literal(None, "DOUBLE") == "NULL"
    assert _sql_literal("true", "BOOLEAN") == "true"
    assert _sql_literal("no", "BOOLEAN") == "false"


def test_insert_sql_builds_value_tuples():
    cols = [{"name": "Region", "type": "VARCHAR"}, {"name": "Sales", "type": "NUMBER(38,2)"}]
    rows = [["West", "10.5"], ["East", "20"]]
    sql = build_dbx_insert_sql("`c`.`s`.`t`", cols, rows)
    assert sql.startswith("INSERT INTO `c`.`s`.`t` (`Region`, `Sales`) VALUES ")
    assert "('West', 10.5)" in sql
    assert "('East', 20)" in sql
