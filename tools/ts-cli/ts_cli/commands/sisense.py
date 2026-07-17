"""ts sisense — Sisense (offline bundle) -> ThoughtSpot conversion commands.

I/O + typer live here; the conversion logic is pure functions in ts_cli/sisense/*.
Conventions (.claude/rules/ts-cli.md): structured JSON to stdout, diagnostics to stderr,
connection by display name (never GUID). Consumes the offline bundle JSON
``{dashboard, widgets, datamodel}`` — no live Sisense/ThoughtSpot connection required.

The MODEL path (parse, build-model) is complete. The LIVEBOARD path (build-liveboard)
computes the full build_from_spec spec + Sisense-local filter chips; its final TML emission
is gated on the shared emitter ``ts_cli.tableau.liveboard.build_from_spec``, which is not yet
on this branch — see the guarded import below.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

# The shared Answer/Liveboard emitter lives on unmerged branches. Guard the import so the
# liveboard path degrades to emitting the computed spec (a valid partial) until it lands.
try:
    from ts_cli.tableau.liveboard import build_from_spec  # type: ignore
except ImportError:  # pragma: no cover - exercised only once the emitter lands on main
    build_from_spec = None

app = typer.Typer(help="Sisense (offline bundle) -> ThoughtSpot conversion commands.")


def _load_bundle(input_file: str) -> dict:
    p = Path(input_file)
    if not p.is_file():
        typer.echo(f"Not a file: {input_file}", err=True)
        raise SystemExit(1)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # malformed JSON -> clear diagnostic, non-zero exit
        typer.echo(f"Could not read bundle JSON: {e}", err=True)
        raise SystemExit(1)


@app.command("parse")
def parse_cmd(
    input_file: str = typer.Option(..., "--input", "-i",
                                   help="Path to the offline Sisense bundle JSON "
                                        "({dashboard, widgets, datamodel})"),
    output_file: str = typer.Option(None, "--output", "-o",
                                    help="Optional path to also write the inventory JSON"),
) -> None:
    """Parse an offline Sisense bundle into a structured inventory.

    Emits tables/columns/relations (from the datamodel) plus a best-effort parse of
    widgets and dashboard filters — the inventory ``build-model`` consumes. The full
    inventory JSON goes to stdout; anything the parser could not confidently read is
    listed under ``warnings`` (echoed to stderr) rather than guessed.
    """
    from ts_cli.sisense.parsing import parse_inventory

    bundle = _load_bundle(input_file)
    inv = parse_inventory(bundle)

    if output_file:
        Path(output_file).write_text(json.dumps(inv, indent=2), encoding="utf-8")

    typer.echo(json.dumps(inv))
    for w in inv.get("warnings", []):
        typer.echo(f"warning: {w}", err=True)


@app.command("build-model")
def build_model_cmd(
    input_file: str = typer.Option(..., "--input", "-i",
                                   help="Path to the offline Sisense bundle JSON"),
    connection: str = typer.Option(..., "--connection", "-c",
                                   help="ThoughtSpot connection display name the tables bind to"),
    database: str = typer.Option(..., "--database", help="Warehouse database"),
    schema: str = typer.Option(..., "--schema", help="Warehouse schema"),
    out: str = typer.Option(..., "--out", "-o",
                            help="Output dir for .tml + mapping.json"),
    model_name: str = typer.Option(None, "--model-name", help="Name for the generated Model"),
    join_type: str = typer.Option("LEFT_OUTER", "--join-type",
                                  help="Join type for relations (LEFT_OUTER keeps fact rows)"),
    lower_db_table: bool = typer.Option(False, "--lower-db-table",
                                        help="Lowercase db_table (Databricks folds unquoted names)"),
    overrides: str = typer.Option(None, "--overrides",
                                  help="overrides.json (connection / model_name / spotter_enabled)"),
) -> None:
    """Build Table + Model TML (and mapping.json) from an offline Sisense bundle.

    Parses the datamodel, emits a Table TML per source table + one Model TML with joins
    (most-connected table = fact, cardinality read from the relation), dedups duplicate
    column names to the fact table, translates any calculated-column formulas, and
    enables Spotter. Serialized via the shared dump_tml_yaml; the connection block
    carries name only (never fqn).
    """
    from ts_cli.tml_common import dump_tml_yaml
    from ts_cli.sisense.parsing import parse_inventory
    from ts_cli.sisense.build_model import assemble

    bundle = _load_bundle(input_file)
    inv = parse_inventory(bundle)
    ov = json.loads(Path(overrides).read_text(encoding="utf-8")) if overrides else {}
    files, mapping = assemble(inv, ov, connection, database, schema, join_type,
                              lower_db_table, model_name)

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, tml in files:
        (out_dir / fname).write_text(dump_tml_yaml(tml), encoding="utf-8")
    (out_dir / "mapping.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")

    mr = mapping["measures"]

    def _n(rows, s):
        return sum(1 for r in rows if r.get("status") == s)
    typer.echo(json.dumps({
        "tables": _n(mapping["tables"], "Migrated"), "model": 1, "measures": len(mr),
        "migrated": _n(mr, "Migrated"), "approximated": _n(mr, "Approximated"),
        "needs_review": _n(mr, "NEEDS REVIEW")}))
    for w in mapping.get("warnings", []):
        typer.echo(f"warning: {w}", err=True)


def _model_names(inv, ov, connection, database, schema, join_type, lower_db_table, model_name):
    """Build the model in-memory to derive the (column_names, measure_names) the report spec
    resolves widget fields against. Mirrors powerbi.build_liveboard's _model_names helper."""
    from ts_cli.sisense.build_model import assemble
    files, _ = assemble(inv, ov, connection or "conn", database, schema, join_type,
                        lower_db_table, model_name)
    model = next(tml for fn, tml in files if fn.endswith(".model.tml"))["model"]
    cols = model["columns"]
    return ([c["name"] for c in cols],
            {c["name"] for c in cols
             if (c.get("properties") or {}).get("column_type") == "MEASURE"})


@app.command("build-liveboard")
def build_liveboard_cmd(
    input_file: str = typer.Option(..., "--input", "-i",
                                   help="Path to the offline Sisense bundle JSON"),
    model_name: str = typer.Option(..., "--model-name",
                                   help="Model name the answers bind to (must match `build-model`)"),
    out: str = typer.Option(None, "--out", "-o",
                            help="Output dir for the emitted .tml (required once the emitter lands)"),
    model_fqn: str = typer.Option(None, "--model-fqn", help="Model GUID to bind to (optional; more robust)"),
    report_name: str = typer.Option(None, "--report-name", help="Liveboard name (default: dashboard title)"),
    connection: str = typer.Option("", "--connection", "-c",
                                   help="Connection name (for the in-memory model build)"),
    database: str = typer.Option("db", "--database"),
    schema: str = typer.Option("schema", "--schema"),
    join_type: str = typer.Option("LEFT_OUTER", "--join-type"),
    lower_db_table: bool = typer.Option(False, "--lower-db-table"),
    overrides: str = typer.Option(None, "--overrides",
                                  help="overrides.json (report_name / extra_visuals)"),
) -> None:
    """Emit Answer + tabbed-Liveboard TML from a Sisense dashboard's widgets, reusing the shared
    build_from_spec (role-aware axes; per-widget ts_chart wins; date buckets; top-N), then inject
    the Sisense dashboard filter bar as Liveboard filter chips.

    NOTE: the shared emitter (ts_cli.tableau.liveboard.build_from_spec) is not yet on this
    branch. Until it lands, this command still computes and emits the full build_from_spec spec
    and the extracted filter chips (JSON to stdout) and exits 0 — a valid partial. Everything
    behind the `if build_from_spec is not None` branch (Answer/Liveboard TML emission + chip
    injection + file writes) auto-activates when the emitter merges to main.
    """
    from ts_cli.sisense.parsing import parse_inventory
    from ts_cli.sisense.answers import spec_from_parse, extract_liveboard_filters
    from ts_cli.sisense.tables import _slug

    bundle = _load_bundle(input_file)
    inv = parse_inventory(bundle)
    ov = json.loads(Path(overrides).read_text(encoding="utf-8")) if overrides else {}

    column_names, measure_names = _model_names(inv, ov, connection, database, schema,
                                               join_type, lower_db_table, model_name)
    spec = spec_from_parse(inv, model_name, model_fqn, column_names, measure_names, ov)
    if report_name:
        spec["report_name"] = report_name
    chips = extract_liveboard_filters(inv, column_names, measure_names)

    for w in inv.get("warnings", []):
        typer.echo(f"warning: {w}", err=True)

    if build_from_spec is None:
        # Emitter absent (unmerged): emit the computed spec + chips, note the pending item, exit 0.
        typer.echo(json.dumps({"status": "spec_only", "spec": spec, "filter_chips": chips}))
        typer.echo(
            "note: final liveboard TML emission is pending the shared emitter "
            "(ts_cli.tableau.liveboard.build_from_spec) landing on main. Emitted the computed "
            "build_from_spec spec + Sisense filter chips only (a valid partial). See "
            "references/open-items.md.", err=True)
        return

    # --- Auto-activates when the emitter lands on main ---------------------------------- #
    from ts_cli.tml_common import dump_tml_yaml

    result = build_from_spec(spec)
    lb_tml = result.get("liveboard")
    if lb_tml and chips:  # inject the Sisense dashboard filter bar as cross-viz chips
        lb_tml["liveboard"]["filters"] = chips

    if not out:
        typer.echo("Provide --out to write the emitted TML.", err=True)
        raise SystemExit(1)
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for a in result.get("answers", []):
        (out_dir / f"{_slug(a['answer']['name'])}.answer.tml").write_text(
            dump_tml_yaml(a), encoding="utf-8")
    if lb_tml:
        (out_dir / f"{_slug(spec['report_name'])}.liveboard.tml").write_text(
            dump_tml_yaml(lb_tml), encoding="utf-8")

    vr = result["visual_rows"]

    def _c(rows, s):
        return sum(1 for r in rows if r.get("status") == s)
    typer.echo(json.dumps({
        "report_name": spec["report_name"], "answers": len(result["answers"]),
        "visuals_migrated": _c(vr, "Migrated"), "approximated": _c(vr, "Approximated"),
        "needs_review": _c(vr, "NEEDS REVIEW"), "filter_chips": len(chips),
        "liveboard": bool(lb_tml)}))
