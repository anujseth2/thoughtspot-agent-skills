"""Unit tests for ts_cli.sisense.functions — JAQL -> ThoughtSpot translation.

Pure functions, no live cluster (per .claude/rules/ts-cli.md). Covers the deterministic
safe subset, the placeholder+agg resolution, case->nested-if and 2-arg-round PARTIAL
caveats, plain-agg translation, and the NEEDS-REVIEW gate for unsupported/unknown funcs.
"""
from ts_cli.sisense.functions import AGG_MAP, FUNCTION_MAP, UNSUPPORTED, translate_agg, translate_jaql


def test_maps_present():
    assert AGG_MAP["sum"] == "SUM"
    assert FUNCTION_MAP["ceiling"] == "ceil"
    assert "rank" in UNSUPPORTED


def test_simple_agg_wrapped_placeholder():
    # sum([rev]) with rev already wrapped -> substitute bare column, map sum
    expr, status, _ = translate_jaql("sum([rev])", {"rev": {"dim": "[Commerce.Revenue]"}})
    assert status == "Migrated"
    assert expr == "sum([Revenue])"


def test_bare_placeholder_applies_context_agg():
    # bare [rev] with a context agg -> agg([Column])
    expr, status, _ = translate_jaql("[rev] / 10", {"rev": {"dim": "[Commerce.Revenue]", "agg": "sum"}})
    assert status == "Migrated"
    assert expr == "sum([Revenue]) / 10"


def test_function_rename():
    expr, status, _ = translate_jaql("ceiling([x])", {"x": {"dim": "[T.Cost]"}})
    assert status == "Migrated"
    assert expr == "ceil([Cost])"


def test_ddiff_to_diff_days():
    expr, status, _ = translate_jaql(
        "ddiff([a], [b])", {"a": {"dim": "[T.Start]"}, "b": {"dim": "[T.End]"}})
    assert status == "Migrated"
    assert expr == "diff_days([Start], [End])"


def test_countduplicates_agg_is_partial():
    expr, status, note = translate_jaql("[v]", {"v": {"dim": "[T.Visit ID]", "agg": "countduplicates"}})
    assert status == "Approximated"
    assert expr == "count([Visit ID])"
    assert "countduplicates" in note


def test_case_maps_to_if_partial():
    expr, status, note = translate_jaql("case([x])", {"x": {"dim": "[T.Cost]"}})
    assert status == "Approximated"
    assert expr == "if([Cost])"
    assert "caveat" in note


def test_round_two_arg_partial():
    expr, status, note = translate_jaql("round([x], 2)", {"x": {"dim": "[T.Cost]"}})
    assert status == "Approximated"
    assert expr.startswith("round([Cost]")
    assert "increment" in note


def test_unsupported_function_needs_review():
    expr, status, note = translate_jaql("rank([x])", {"x": {"dim": "[T.Cost]"}})
    assert expr is None
    assert status == "NEEDS REVIEW"
    assert "rank" in note


def test_unknown_function_needs_review():
    expr, status, note = translate_jaql("foobar([x])", {"x": {"dim": "[T.Cost]"}})
    assert expr is None
    assert status == "NEEDS REVIEW"
    assert "foobar" in note


def test_nested_formula_recurses():
    ctx = {"m": {"formula": "sum([r])", "context": {"r": {"dim": "[T.Revenue]"}}}}
    expr, status, _ = translate_jaql("[m] * 2", ctx)
    assert status == "Migrated"
    assert expr == "(sum([Revenue])) * 2"


def test_translate_agg_simple_and_review():
    assert translate_agg("sum") == ("SUM", "Migrated", "")
    kw, status, _ = translate_agg("countduplicates")
    assert (kw, status) == ("COUNT", "Approximated")
    kw, status, note = translate_agg("median")
    assert kw is None and status == "NEEDS REVIEW" and "median" in note
