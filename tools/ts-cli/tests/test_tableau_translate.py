"""Unit tests for the Tableau → ThoughtSpot formula translation engine.

Test cases derived from actual CPG Merch Promotion Performance migration failures.
"""
from __future__ import annotations

import pytest

from ts_cli.tableau_translate import (
    build_calc_id_map,
    build_dependency_dag,
    convert_case_when,
    convert_if_then,
    convert_iif,
    convert_int,
    convert_lod,
    convert_string_concat,
    convert_total,
    detect_param_conflicts,
    ensure_else_clause,
    map_date_functions,
    map_functions,
    map_parameter_names,
    resolve_cross_references,
    scope_columns,
    strip_parameter_prefix,
    translate_formulas,
    translate_single,
    validate_output,
)


# ---------------------------------------------------------------------------
# Parameter handling
# ---------------------------------------------------------------------------

class TestStripParameterPrefix:
    def test_basic(self):
        assert strip_parameter_prefix("[Parameters].[Metric]") == "[Metric]"

    def test_inline(self):
        result = strip_parameter_prefix(
            "IF [Parameters].[Parameter 3 1]='Sales' THEN [X] END"
        )
        assert "[Parameters]." not in result
        assert "[Parameter 3 1]" in result

    def test_no_params(self):
        assert strip_parameter_prefix("[Sales] + [Revenue]") == "[Sales] + [Revenue]"

    def test_case_insensitive(self):
        assert strip_parameter_prefix("[parameters].[Foo]") == "[Foo]"


class TestMapParameterNames:
    def test_basic(self):
        result = map_parameter_names(
            "[Parameter 3 1]",
            {"Parameter 3 1": "Metric"},
        )
        assert result == "[Metric]"

    def test_multiple(self):
        result = map_parameter_names(
            "IF [Parameter 3 1]='Sales' THEN [Parameter 6] END",
            {"Parameter 3 1": "Metric", "Parameter 6": "Engagement Type"},
        )
        assert "[Metric]" in result
        assert "[Engagement Type]" in result


# ---------------------------------------------------------------------------
# CASE/WHEN conversion
# ---------------------------------------------------------------------------

class TestConvertCaseWhen:
    def test_basic_case(self):
        expr = "CASE [Parameters].[Metric]\nWHEN 'Sales' THEN [SALES]\nWHEN 'Revenue' THEN [REVENUE]\nEND"
        result = convert_case_when(expr)
        assert "if" in result
        assert "CASE" not in result
        assert "WHEN" not in result
        assert "[Parameters].[Metric] = 'Sales'" in result

    def test_case_with_else(self):
        expr = "CASE [X] WHEN 'a' THEN 1 WHEN 'b' THEN 2 ELSE 0 END"
        result = convert_case_when(expr)
        assert "if ( [X] = 'a' ) then 1" in result
        assert "else if ( [X] = 'b' ) then 2" in result
        assert "else 0" in result
        assert "END" not in result


# ---------------------------------------------------------------------------
# IF/THEN/END conversion
# ---------------------------------------------------------------------------

class TestConvertIfThen:
    def test_basic_if(self):
        result = convert_if_then("IF [X] > 5 THEN 'High' ELSE 'Low' END")
        assert "if ( [X] > 5 ) then" in result
        assert "END" not in result
        assert "'High'" in result

    def test_nested_if(self):
        result = convert_if_then(
            "IF [X]='a' THEN 1 ELSEIF [X]='b' THEN 2 ELSE 3 END"
        )
        assert "if ( [X]='a' ) then 1" in result
        assert "else if ( [X]='b' ) then 2" in result
        assert "else 3" in result
        assert "END" not in result
        assert "ELSEIF" not in result

    def test_preserves_non_if(self):
        result = convert_if_then("[Sales] + [Revenue]")
        assert result == "[Sales] + [Revenue]"


# ---------------------------------------------------------------------------
# IIF conversion
# ---------------------------------------------------------------------------

class TestConvertIif:
    def test_basic(self):
        result = convert_iif("IIF([X] > 0, 'Positive', 'Negative')")
        assert "if ( [X] > 0 ) then 'Positive' else 'Negative'" in result

    def test_no_iif(self):
        assert convert_iif("[Sales]") == "[Sales]"


# ---------------------------------------------------------------------------
# Function mapping
# ---------------------------------------------------------------------------

class TestMapFunctions:
    def test_countd(self):
        result = map_functions("COUNTD([Customer])")
        assert "unique count" in result
        assert "[Customer]" in result

    def test_avg(self):
        result = map_functions("AVG([Sales])")
        assert "average ( [Sales])" in result

    def test_zn(self):
        result = map_functions("ZN([Sales])")
        assert "ifnull ( [Sales] , 0 )" in result

    def test_contains(self):
        result = map_functions("CONTAINS([Name], 'test')")
        assert "contains ( [Name], 'test')" in result

    def test_len(self):
        result = map_functions("LEN([Name])")
        assert "strlen ( [Name])" in result

    def test_sum_preserved(self):
        result = map_functions("SUM([Sales])")
        assert "sum ( [Sales])" in result

    def test_nested_zn(self):
        result = map_functions("ZN([X]) + ZN([Y])")
        assert "ifnull ( [X] , 0 )" in result
        assert "ifnull ( [Y] , 0 )" in result


# ---------------------------------------------------------------------------
# Date function mapping
# ---------------------------------------------------------------------------

class TestMapDateFunctions:
    def test_datetrunc_month(self):
        result = map_date_functions("DATETRUNC('month', [Date])")
        assert "start_of_month ( [Date] )" in result

    def test_datetrunc_quarter(self):
        result = map_date_functions("DATETRUNC('quarter', [Date])")
        assert "start_of_quarter ( [Date] )" in result

    def test_datediff_day(self):
        result = map_date_functions("DATEDIFF('day', [Start], [End])")
        assert "diff_days ( [End] , [Start] )" in result

    def test_datediff_reversed_args(self):
        result = map_date_functions("DATEDIFF('month', [A], [B])")
        # TS takes (end, start) — reversed from Tableau
        assert "diff_months ( [B] , [A] )" in result

    def test_dateadd_day(self):
        result = map_date_functions("DATEADD('day', 1, [Date])")
        assert "add_days ( [Date] , 1 )" in result

    def test_datepart_month(self):
        result = map_date_functions("DATEPART('month', [Date])")
        assert "month_number ( [Date] )" in result

    def test_datepart_weekday(self):
        result = map_date_functions("DATEPART('weekday', [Date])")
        assert "day_of_week ( [Date] )" in result

    def test_datediff_hour(self):
        result = map_date_functions("DATEDIFF('hour', [A], [B])")
        assert "diff_time ( [B] , [A] ) / 3600" in result


# ---------------------------------------------------------------------------
# INT conversion
# ---------------------------------------------------------------------------

class TestConvertInt:
    def test_int(self):
        result = convert_int("INT([X])")
        assert "floor" in result
        assert "ceil" in result
        assert ">= 0" in result


# ---------------------------------------------------------------------------
# String concatenation
# ---------------------------------------------------------------------------

class TestConvertStringConcat:
    def test_string_plus(self):
        result = convert_string_concat("to_string([X]) + '%'", role="dimension")
        assert "concat" in result
        assert "+" not in result

    def test_numeric_plus_preserved(self):
        result = convert_string_concat("[Sales] + [Revenue]", role="measure")
        assert "+" in result
        assert "concat" not in result


# ---------------------------------------------------------------------------
# Column scoping
# ---------------------------------------------------------------------------

class TestScopeColumns:
    def test_basic_scoping(self):
        result = scope_columns(
            "[SALES] + [REVENUE]",
            {"SALES": "ORDERS", "REVENUE": "ORDERS"},
        )
        assert "[ORDERS::SALES]" in result
        assert "[ORDERS::REVENUE]" in result

    def test_already_scoped(self):
        result = scope_columns(
            "[ORDERS::SALES]",
            {"SALES": "ORDERS"},
        )
        assert result == "[ORDERS::SALES]"

    def test_formula_names_excluded(self):
        result = scope_columns(
            "[My Formula] + [SALES]",
            {"SALES": "ORDERS", "My Formula": "ORDERS"},
            formula_names={"My Formula"},
        )
        assert "[My Formula]" in result  # not scoped
        assert "[ORDERS::SALES]" in result  # scoped

    def test_parameter_names_excluded(self):
        result = scope_columns(
            "[Metric] + [SALES]",
            {"SALES": "ORDERS"},
            parameter_names={"Metric"},
        )
        assert "[Metric]" in result  # not scoped
        assert "[ORDERS::SALES]" in result


# ---------------------------------------------------------------------------
# Mandatory else clause
# ---------------------------------------------------------------------------

class TestEnsureElseClause:
    def test_adds_else_for_measure(self):
        result = ensure_else_clause(
            "if ( [X] > 5 ) then [Sales]",
            role="measure",
        )
        assert "else 0" in result

    def test_adds_else_for_dimension(self):
        result = ensure_else_clause(
            "if ( [X] > 5 ) then 'High'",
            role="dimension",
        )
        assert "else ''" in result

    def test_preserves_existing_else(self):
        expr = "if ( [X] > 5 ) then 'High' else 'Low'"
        result = ensure_else_clause(expr, role="dimension")
        assert result == expr


# ---------------------------------------------------------------------------
# LOD expression conversion
# ---------------------------------------------------------------------------

class TestConvertLod:
    def test_fixed_single_dim(self):
        result = convert_lod("{FIXED [Dim] : SUM([Sales])}")
        assert "group_aggregate ( SUM([Sales]) , { [Dim] } , {} )" in result

    def test_fixed_multi_dim(self):
        result = convert_lod("{FIXED [D1], [D2] : AVG([X])}")
        assert "group_aggregate" in result
        assert "{ [D1] , [D2] }" in result

    def test_include(self):
        result = convert_lod("{INCLUDE [Dim] : SUM([X])}")
        assert "query_groups () + { [Dim] }" in result

    def test_exclude(self):
        result = convert_lod("{EXCLUDE [Dim] : SUM([X])}")
        assert "query_groups () - { [Dim] }" in result

    def test_grand_fixed(self):
        result = convert_lod("{FIXED : MAX([Date])}")
        assert "group_aggregate ( MAX([Date]) , {} , {} )" in result


# ---------------------------------------------------------------------------
# TOTAL conversion
# ---------------------------------------------------------------------------

class TestConvertTotal:
    def test_basic(self):
        result = convert_total("TOTAL(SUM([Sales]))")
        assert "group_aggregate ( SUM([Sales]) , {} , query_filters () )" in result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateOutput:
    def test_clean(self):
        assert validate_output("if ( [X] > 5 ) then [Sales] else 0") == []

    def test_bare_end(self):
        errors = validate_output("if ( [X] > 5 ) then [Sales] END")
        assert any("END" in e for e in errors)

    def test_bare_case(self):
        errors = validate_output("CASE [X] WHEN 'a' THEN 1")
        assert any("CASE" in e for e in errors)

    def test_unique_count_underscore(self):
        errors = validate_output("unique_count([X])")
        assert any("unique_count" in e for e in errors)


# ---------------------------------------------------------------------------
# Parameter conflict detection
# ---------------------------------------------------------------------------

class TestDetectParamConflicts:
    def test_pass_through(self):
        formulas = [{"caption": "Metric", "formula": "[Parameters].[Metric]"}]
        params = [{"caption": "Metric"}]
        conflicts = detect_param_conflicts(formulas, params)
        assert "Metric" in conflicts
        assert "pass-through" in conflicts["Metric"]

    def test_substantive_formula(self):
        formulas = [{"caption": "Metric", "formula": "IF [Parameters].[Metric]='A' THEN 1 END"}]
        params = [{"caption": "Metric"}]
        conflicts = detect_param_conflicts(formulas, params)
        assert "Metric" in conflicts
        assert "collision" in conflicts["Metric"]

    def test_no_conflict(self):
        formulas = [{"caption": "Sales Total", "formula": "SUM([Sales])"}]
        params = [{"caption": "Metric"}]
        conflicts = detect_param_conflicts(formulas, params)
        assert conflicts == {}


# ---------------------------------------------------------------------------
# Dependency DAG
# ---------------------------------------------------------------------------

class TestBuildDependencyDag:
    def test_level_assignment(self):
        formulas = [
            {"caption": "Base", "name": "Calculation_1", "formula": "SUM([Sales])"},
            {"caption": "Derived", "name": "Calculation_2",
             "formula": "[Calculation_1] * 2"},
        ]
        dag = build_dependency_dag(formulas)
        assert dag["Base"]["level"] == 0
        assert dag["Derived"]["level"] == 1

    def test_no_deps(self):
        formulas = [
            {"caption": "Simple", "name": "Calculation_1", "formula": "[Sales] + 1"},
        ]
        dag = build_dependency_dag(formulas)
        assert dag["Simple"]["level"] == 0


# ---------------------------------------------------------------------------
# Full translate_single pipeline
# ---------------------------------------------------------------------------

class TestTranslateSingle:
    def test_simple_if(self):
        expr, errors = translate_single(
            "IF [PERIOD_TYPE]='pre' THEN [CPG_SALES] END",
            role="measure",
        )
        assert "if" in expr
        assert "END" not in expr
        assert "else 0" in expr
        assert errors == []

    def test_case_with_params(self):
        expr, errors = translate_single(
            "CASE [Parameters].[Parameter 3 1]\nWHEN 'Sales' THEN [SALES]\nWHEN 'Revenue' THEN [REVENUE]\nEND",
            role="measure",
            param_map={"Parameter 3 1": "Metric"},
        )
        assert "[Parameters]" not in expr
        assert "CASE" not in expr
        assert "WHEN" not in expr
        assert "[Metric]" in expr
        assert errors == []

    def test_datetrunc_date(self):
        expr, errors = translate_single(
            "DATE(DATETRUNC('month', [DATE]))",
            role="dimension",
        )
        assert "start_of_month" in expr
        assert "date" in expr
        assert errors == []

    def test_zn_expression(self):
        expr, errors = translate_single(
            "ZN([Sales]) + ZN([Revenue])",
            role="measure",
        )
        assert "ifnull" in expr
        assert "ZN" not in expr

    def test_column_scoping(self):
        expr, errors = translate_single(
            "SUM([SALES])",
            role="measure",
            scoped_columns={"SALES": "ORDERS"},
        )
        assert "[ORDERS::SALES]" in expr

    def test_datediff_reorder(self):
        expr, errors = translate_single(
            "DATEDIFF('day', [Start], [End])",
            role="measure",
        )
        assert "diff_days ( [End] , [Start] )" in expr


# ---------------------------------------------------------------------------
# Full pipeline: translate_formulas batch
# ---------------------------------------------------------------------------

class TestTranslateFormulas:
    def test_basic_batch(self):
        formulas = [
            {
                "caption": "Sales Pre",
                "name": "Calculation_1",
                "formula": "IF [PERIOD]='pre' THEN [SALES] END",
                "role": "measure",
                "datatype": "real",
                "datasource": "test",
            },
            {
                "caption": "Revenue",
                "name": "Calculation_2",
                "formula": "SUM([REVENUE])",
                "role": "measure",
                "datatype": "real",
                "datasource": "test",
            },
        ]
        result = translate_formulas(formulas)
        assert result["stats"]["total"] == 2
        assert result["stats"]["translated"] == 2
        assert result["stats"]["skipped"] == 0
        assert len(result["translated"]) == 2

    def test_cross_reference_resolution(self):
        formulas = [
            {
                "caption": "Base Sales",
                "name": "Calculation_100",
                "formula": "SUM([SALES])",
                "role": "measure",
                "datatype": "real",
                "datasource": "test",
            },
            {
                "caption": "Percent of Sales",
                "name": "Calculation_200",
                "formula": "[Calculation_100] / 100",
                "role": "measure",
                "datatype": "real",
                "datasource": "test",
            },
        ]
        result = translate_formulas(formulas)
        assert result["stats"]["translated"] == 2
        # The derived formula should have resolved the cross-reference
        derived = next(t for t in result["translated"] if t["name"] == "Percent of Sales")
        assert "Calculation_" not in derived["expr"]

    def test_param_conflict_passthrough_skipped(self):
        formulas = [
            {
                "caption": "Metric",
                "name": "Calculation_1",
                "formula": "[Parameters].[Metric]",
                "role": "dimension",
                "datatype": "string",
                "datasource": "test",
            },
        ]
        parameters = [{"caption": "Metric"}]
        result = translate_formulas(formulas, parameters=parameters)
        assert result["stats"]["skipped"] == 1
        assert "pass-through" in result["skipped"][0]["reason"]

    def test_stats_include_levels(self):
        formulas = [
            {
                "caption": "A",
                "name": "Calculation_1",
                "formula": "[Sales]",
                "role": "measure",
                "datatype": "real",
                "datasource": "test",
            },
        ]
        result = translate_formulas(formulas)
        assert 0 in result["stats"]["levels"]
