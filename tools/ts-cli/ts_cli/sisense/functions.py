"""Sisense JAQL -> ThoughtSpot formula translation (deterministic safe subset).

Ported from the standalone converter (map/formula.py). Pure functions, no I/O.
translate_jaql(expr, context=None) -> (expr_out, status, note); expr_out is None for
NEEDS REVIEW (the caller preserves the original Sisense formula). translate_agg(agg)
translates a plain JAQL aggregation (no formula) to a TML aggregation keyword.

STRATEGY (unchanged from the standalone converter): deterministically translate the
common subset; emit everything else as NEEDS REVIEW with the original formula preserved.
The long tail (time-intelligence, RANK/ORDERING, measured-value scoping, R) is out of
scope by design.

Coverage -> status mapping (mirrors the Power BI converter's three statuses):
  AUTO -> "Migrated"; PARTIAL -> "Approximated"; MANUAL -> "NEEDS REVIEW".
"""
from __future__ import annotations

import re

# Sisense JAQL `agg` -> TML aggregation property (for SIMPLE measures, no formula).
AGG_MAP: dict[str, str] = {
    "sum": "SUM",
    "avg": "AVERAGE",
    "count": "COUNT",
    "countduplicates": "COUNT",   # approx (DupCount); flag PARTIAL
    "min": "MIN",
    "max": "MAX",
    "stdev": "STD_DEVIATION",
    "var": "VARIANCE",
    # median / stdevp / varp / mode have no clean TML aggregation -> MANUAL
}

# Sisense formula function -> TML formula function (deterministic 1:1 subset only).
FUNCTION_MAP: dict[str, str] = {
    # aggregation
    "sum": "sum",
    "avg": "average",
    "average": "average",
    "count": "count",
    "min": "min",
    "max": "max",
    # mathematical
    "abs": "abs",
    "round": "round",
    "ceiling": "ceil",
    "floor": "floor",
    "power": "pow",
    "sqrt": "sqrt",
    "exp": "exp",
    "mod": "mod",
    "log": "ln",       # Sisense `Log` is the NATURAL log (Sisense has no separate `Ln`)
    "ln": "ln",        # defensive alias if a JAQL variant uses `ln`
    "log10": "log10",
    "sign": "sign",
    # date difference -- Sisense DDiff(d1, d2) -> ThoughtSpot diff_days(d1, d2)
    "ddiff": "diff_days",
    # statistical (sample variants)
    "stdev": "stddev",
    "var": "variance",
    "median": "median",
    # logical / conditional
    "if": "if",
    "isnull": "isnull",    # TS spells it `isnull` (NOT `is_null`)
    "ifnull": "ifnull",
    # "case" -> handled specially (maps to nested if) -> PARTIAL; see _PARTIAL_FUNCS
}

# Functions we will NOT auto-translate. Presence => NEEDS REVIEW. Unknown functions are
# NEEDS REVIEW anyway; this set exists for clearer notes and to guard names that look
# translatable but are not (population stats, R, window, time-intelligence).
UNSUPPORTED: frozenset = frozenset({
    # window / ranking
    "rank", "ordering", "rsum", "rpsum", "rpavg", "prev", "next", "all", "now",
    # time intelligence: period-to-date
    "ytdsum", "ytdavg", "mtdsum", "mtdavg", "qtdsum", "qtdavg", "wtdsum",
    # time intelligence: prior period
    "pastday", "pastweek", "pastmonth", "pastquarter", "pastyear",
    # time intelligence: growth / diff (ddiff is supported -> diff_days, see FUNCTION_MAP)
    "growth", "growthrate", "diffpastyear", "diffpastmonth", "growthpastyear",
    "ydiff", "qdiff", "mdiff", "hdiff", "mndiff", "sdiff",
    # population / advanced statistics (no confident TML 1:1)
    "stdevp", "varp", "mode", "largest", "smallest",
    "percentile", "quartile", "correl", "covar", "slope",
    # R integration
    "rdouble", "rint",
})

# Functions that translate but with a caveat worth a human review -> PARTIAL.
_PARTIAL_FUNCS: frozenset = frozenset({"case"})

# identifier immediately followed by "(" -> a function call in the expression.
_FUNC_CALL = re.compile(r"([A-Za-z_]\w*)\s*\(")

# Coverage levels used internally; mapped to status strings on the way out.
_AUTO, _PARTIAL, _MANUAL = "AUTO", "PARTIAL", "MANUAL"
_STATUS = {_AUTO: "Migrated", _PARTIAL: "Approximated", _MANUAL: "NEEDS REVIEW"}


def _column_from_dim(dim: str | None) -> str | None:
    """Sisense dim '[Orders.Revenue]' -> TML column ref '[Revenue]'.

    Strips the surrounding brackets and the 'Table.' qualifier, keeping the display
    name (spaces and all), and drops a trailing date-hierarchy tag so the ref matches
    the base model column (e.g. "Date(Calendar)" -> "Date").
    """
    if not dim:
        return None
    s = dim.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    s = s.split(".")[-1]
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    return "[" + s + "]"


def _agg_to_func(agg: str) -> tuple:
    """A JAQL agg used as a formula wrapper -> (tml_func|None, coverage, note)."""
    key = (agg or "").lower()
    if key == "countduplicates":
        return "count", _PARTIAL, "countduplicates approximated as count"
    if key in FUNCTION_MAP:           # sum, avg->average, count, min, max
        return FUNCTION_MAP[key], _AUTO, ""
    return None, _MANUAL, f"no TML function for agg '{agg}'"


def _normalize_key(raw_key: str) -> str:
    """Context keys may be bracketed ('[rev]') or bare ('rev'); normalize to bare."""
    k = raw_key.strip()
    if k.startswith("[") and k.endswith("]"):
        k = k[1:-1]
    return k


def translate_jaql(expr, context: dict | None = None) -> tuple:
    """Translate a Sisense JAQL formula + context into a TML formula expression.

    Steps (per the standalone converter):
      1. Resolve each `[key]` placeholder against the context. A `{dim, agg}` fragment
         becomes a column ref `[Column]` when the expression already wraps it in an
         aggregation, or `agg([Column])` when it appears bare. Nested `formula`
         fragments recurse.
      2. Map function names via FUNCTION_MAP.
      3. Any function in UNSUPPORTED (or unknown), or an unresolvable placeholder, makes
         the whole formula NEEDS REVIEW (expr None) with the offender noted.
      4. `case` / `countduplicates` / 2-arg `round` -> Approximated with a caveat.

    Returns (expr_out, status, note); expr_out is None when status == "NEEDS REVIEW".
    """
    source = expr or ""
    out = source
    context = context or {}
    coverage = _AUTO
    notes: list = []

    def downgrade(level: str, note: str = "") -> None:
        nonlocal coverage
        if note:
            notes.append(note)
        # MANUAL is the floor; PARTIAL only downgrades from AUTO.
        if level == _MANUAL or coverage == _AUTO:
            coverage = level

    # 1. Resolve context placeholders.
    for raw_key, frag in context.items():
        key = _normalize_key(raw_key)
        token = "[" + key + "]"
        frag = frag if isinstance(frag, dict) else {}

        if frag.get("formula"):  # nested calc -> recurse
            sub_expr, sub_status, sub_note = translate_jaql(str(frag["formula"]),
                                                            frag.get("context") or {})
            if sub_status == "NEEDS REVIEW" or sub_expr is None:
                return None, "NEEDS REVIEW", sub_note or f"unsupported nested formula for '{key}'"
            if sub_status == "Approximated":
                downgrade(_PARTIAL, sub_note)
            out = out.replace(token, "(" + sub_expr + ")")
            continue

        col = _column_from_dim(frag.get("dim"))
        if col is None:
            return None, "NEEDS REVIEW", f"cannot resolve placeholder '{key}' (no dim/formula)"

        # If the expression already aggregates the placeholder (e.g. "sum([rev])"),
        # substitute the bare column and let step 2 map the wrapping function. If it
        # appears bare, apply the context agg here.
        wrapped = re.search(r"[A-Za-z_]\w*\s*\(\s*" + re.escape(token) + r"\s*\)", source)
        agg = frag.get("agg")
        if wrapped or not agg:
            replacement = col
        else:
            fn, cov, note = _agg_to_func(agg)
            if fn is None:
                return None, "NEEDS REVIEW", note
            downgrade(cov, note)
            replacement = f"{fn}({col})"
        out = out.replace(token, replacement)

    # 2/3. Inspect every function call in the (original) expression.
    for name in _FUNC_CALL.findall(source):
        low = name.lower()
        if low in UNSUPPORTED:
            return None, "NEEDS REVIEW", f"unsupported function '{name}'"
        if low in FUNCTION_MAP:
            continue
        if low in _PARTIAL_FUNCS:
            downgrade(_PARTIAL, f"'{name}' mapped with a caveat (review)")
            continue
        return None, "NEEDS REVIEW", f"unknown function '{name}'"

    # 3b. round() arg semantics diverge: TS's 2nd arg is a rounding INCREMENT
    # (round(x, .01) for 2 decimals), not Sisense's decimal-place COUNT (Round(x, 2)).
    if re.search(r"\bround\s*\([^()]*,", source, re.IGNORECASE):
        downgrade(_PARTIAL,
                  "TS round() 2nd arg is a rounding increment, not a decimal-place count")

    # 4. Rename mapped functions in the resolved expression (ceiling->ceil, case->if).
    def _rename(m: re.Match) -> str:
        low = m.group(1).lower()
        if low == "case":
            return "if("
        return FUNCTION_MAP.get(low, m.group(1)) + "("

    out = _FUNC_CALL.sub(_rename, out)
    out = re.sub(r"\s+", " ", out).strip()

    return out, _STATUS[coverage], "; ".join(notes)


def translate_agg(agg) -> tuple:
    """Translate a plain JAQL `agg` (no formula) to a TML aggregation keyword.

    Returns (agg_keyword|None, status, note); agg_keyword is None for NEEDS REVIEW.
    """
    key = (agg or "").lower()
    if key in AGG_MAP:
        status = "Approximated" if key == "countduplicates" else "Migrated"
        note = "countduplicates approximated as COUNT" if key == "countduplicates" else ""
        return AGG_MAP[key], status, note
    return None, "NEEDS REVIEW", f"no TML aggregation for Sisense agg '{agg}'"
