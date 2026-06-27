"""Unit tests for the Tableau published-reference resolver (pure functions only).

No live connection required — exercises find_references / parse_tds_structure /
parse_vc / caption_fallback against inline fixtures.
"""
import json

from ts_cli.tableau_resolve import (
    find_references, parse_tds_structure, parse_vc, caption_fallback,
)

SQLPROXY_TWB = """<?xml version='1.0'?>
<workbook>
  <datasources>
    <datasource caption='Orders Demo PubDS' name='federated.x'>
      <repository-location id='OrdersDemoPubDS' site='ts-skills' path='/t/ts-skills/datasources'/>
      <connection class='sqlproxy' dbname='OrdersDemoPubDS'>
        <relation name='sqlproxy' table='[sqlproxy]' type='table'/>
      </connection>
    </datasource>
  </datasources>
</workbook>"""

VC_TWB = """<?xml version='1.0'?>
<workbook>
  <datasources>
    <datasource caption='orders_demo (workspace.tableau_repro.orders_demo) (DB Test)' name='federated.y'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='DB Test' name='publishedConnection.z'>
            <connection class='publishedConnection' resourceId='abc-123' resourceName='DB Test'/>
          </named-connection>
        </named-connections>
      </connection>
    </datasource>
  </datasources>
</workbook>"""

TDS = """<?xml version='1.0'?>
<datasource>
  <connection class='federated'>
    <named-connections>
      <named-connection caption='dbc'>
        <connection class='databricks' server='dbc-x.databricks.com' dbname='workspace'/>
      </named-connection>
    </named-connections>
    <relation name='orders_demo' table='[workspace].[tableau_repro].[orders_demo]' type='table'/>
    <metadata-records>
      <metadata-record class='column'>
        <remote-name>sales</remote-name><local-name>[sales]</local-name>
        <parent-name>[orders_demo]</parent-name><local-type>real</local-type>
      </metadata-record>
    </metadata-records>
  </connection>
</datasource>"""


def test_find_references_sqlproxy():
    refs = find_references(SQLPROXY_TWB)
    assert len(refs) == 1
    assert refs[0]["kind"] == "published_datasource"
    assert refs[0]["repo_id"] == "OrdersDemoPubDS"
    assert refs[0]["dbname"] == "OrdersDemoPubDS"


def test_find_references_virtual_connection():
    refs = find_references(VC_TWB)
    assert len(refs) == 1
    assert refs[0]["kind"] == "virtual_connection"
    assert refs[0]["resource_id"] == "abc-123"
    assert refs[0]["resource_name"] == "DB Test"


def test_parse_tds_structure():
    spec = parse_tds_structure(TDS)
    assert spec["db_class"] == "databricks"
    t = spec["tables"][0]
    assert t["database"] == "workspace" and t["schema"] == "tableau_repro"
    assert t["columns"][0]["name"] == "sales"
    assert t["columns"][0]["ts_type"] == "DOUBLE"  # local-type 'real' -> DOUBLE


def test_parse_vc():
    content = {
        "policyCollection": {"policies": [{"x": 1}]},
        "revision": {"revisableProperties": {"tables": [
            {"revisableProperties": {
                "physicalIdentifier": {"databaseName": "workspace", "schemaName": "tableau_repro",
                                       "qualifiedName": "[workspace].[tableau_repro].[orders_demo]"},
                "columns": [{"revisableProperties": {"name": "Sales", "physicalColumnName": "sales",
                                                     "dataType": "DT_REAL"}}]}}]}}}
    detail = {"virtualConnection": {"content": json.dumps(content)}}
    conns = {"virtualConnectionConnections": {"connection": [{"dbClass": "databricks", "server": "h"}]}}
    spec = parse_vc(detail, conns)
    assert spec["db_class"] == "databricks"
    assert spec["rls_policy_count"] == 1
    assert spec["tables"][0]["columns"][0]["ts_type"] == "DOUBLE"  # DT_REAL -> DOUBLE


def test_caption_fallback():
    assert caption_fallback("Name (a.b.c) (x)") == "a.b.c"
    assert caption_fallback("no qualified name") is None
