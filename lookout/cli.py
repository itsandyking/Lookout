"""Unified CLI for Lookout — merchandising command center."""

import asyncio
import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

console = Console()


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with rich output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(console=console, rich_tracebacks=True, show_path=False),
        ],
    )
    for lib in ("httpx", "httpcore", "playwright", "anthropic"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def find_vendors_yaml(explicit_path: Path | None = None) -> Path | None:
    """Find vendors.yaml, checking explicit path, cwd, then package dir."""
    if explicit_path and explicit_path.exists():
        return explicit_path
    candidates = [Path.cwd() / "vendors.yaml", Path(__file__).parent.parent / "vendors.yaml"]
    for c in candidates:
        if c.exists():
            return c
    return None


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    """Lookout — merchandising command center for TMA."""


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--vendor", "-v", default=None, help="Filter by vendor")
@click.option(
    "--out", "-o", "output_path", type=click.Path(path_type=Path), default=None,
    help="Export priority CSV",
)
@click.option("--include-house-brands", is_flag=True, help="Include house brands in audit")
@click.option("--verbose", is_flag=True)
def audit(vendor, output_path, include_house_brands, verbose):
    """Run content audit — find products with gaps."""
    setup_logging(verbose)
    from lookout.audit.auditor import ContentAuditor
    from lookout.store import LookoutStore

    try:
        store = LookoutStore()
    except Exception as e:
        console.print(f"[red]Error connecting to database:[/red] {e}")
        sys.exit(1)

    auditor = ContentAuditor(store, exclude_house_brands=not include_house_brands)
    result = auditor.audit(vendor=vendor)
    summary = result.summary()

    table = Table(title="Content Audit Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total Products", str(summary["total_products"]))
    table.add_row("Products with Gaps", str(summary["products_with_gaps"]))
    table.add_row("Complete", str(summary["products_complete"]))
    table.add_row("Completion %", f"{summary['completion_pct']}%")
    table.add_row("Missing Images", str(summary["missing_images"]))
    table.add_row("Missing Variant Images", str(summary["missing_variant_images"]))
    table.add_row("Missing Descriptions", str(summary["missing_description"]))
    console.print(table)

    if output_path:
        csv_bytes = result.to_priority_csv()
        output_path.write_bytes(csv_bytes)
        console.print(f"\nPriority CSV written to [green]{output_path}[/green]")


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------


@cli.group()
def enrich():
    """Content enrichment pipeline."""


@enrich.command()
@click.option(
    "-i", "--input", "input_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="Input CSV (optional — audits internally if not provided)",
)
@click.option("--vendor", "-v", default=None, help="Filter by vendor (used with internal audit)")
@click.option(
    "--out", "-o", "output_dir", type=click.Path(path_type=Path), default=Path("./output"),
    help="Output directory",
)
@click.option(
    "--vendors", "vendors_path", type=click.Path(exists=True, path_type=Path), default=None,
    help="Path to vendors.yaml",
)
@click.option("--concurrency", "-c", type=int, default=5, help="Max concurrent requests")
@click.option("--max-rows", "-n", type=int, default=None, help="Max rows to process")
@click.option("--force", "-f", is_flag=True, help="Force re-processing")
@click.option("--dry-run", is_flag=True, help="Process but don't write output")
@click.option("--verify", is_flag=True, help="Enable LLM fact-checking of generated descriptions")
@click.option("--only", "only_mode", type=click.Choice(["images", "description", "variant-images"]), default=None, help="Only fill this specific gap type")
@click.option("--verbose", is_flag=True)
def run(input_path, vendor, output_dir, vendors_path, concurrency, max_rows, force, dry_run, verify, only_mode, verbose):
    """Run the enrichment pipeline."""
    setup_logging(verbose)
    from lookout.enrich.pipeline import PipelineConfig, run_pipeline

    vendors_path = find_vendors_yaml(vendors_path)
    if not vendors_path:
        console.print("[red]Error:[/red] vendors.yaml not found")
        sys.exit(1)

    # If no input CSV, run audit internally to get priorities with rich variant data
    enriched_rows = None
    if input_path is None and vendor:
        console.print(f"[dim]Running internal audit for vendor: {vendor}[/dim]")

        from lookout.audit.auditor import ContentAuditor
        from lookout.store import LookoutStore

        store = LookoutStore()
        auditor = ContentAuditor(store, exclude_house_brands=True)
        result = auditor.audit(vendor=vendor)

        if not result.priority_items:
            console.print("[yellow]No products with gaps found.[/yellow]")
            return

        # Convert to InputRows with full variant data + catalog images
        enriched_rows = result.to_input_rows(store=store, max_rows=max_rows)
        console.print(
            f"[dim]Found {len(result.priority_items)} products with gaps, "
            f"enriching {len(enriched_rows)} with full variant data[/dim]"
        )

        # Count catalog images available
        catalog_count = sum(1 for r in enriched_rows if r.catalog_images_by_color)
        if catalog_count:
            console.print(f"[dim]Catalog images available for {catalog_count} products[/dim]")

    if input_path is None and enriched_rows is None:
        console.print("[red]Error:[/red] Provide --input CSV or --vendor for internal audit")
        sys.exit(1)

    config = PipelineConfig(
        input_path=input_path,
        output_dir=output_dir,
        input_rows=enriched_rows or [],
        vendors_path=vendors_path,
        concurrency=concurrency,
        max_rows=max_rows,
        force=force,
        dry_run=dry_run,
        verify=verify,
        only_mode=only_mode,
    )

    console.print(Panel(
        f"[bold]Input:[/bold] {input_path}\n"
        f"[bold]Output:[/bold] {output_dir}\n"
        f"[bold]Vendors:[/bold] {vendors_path}\n"
        f"[bold]Concurrency:[/bold] {concurrency}",
        title="Enrichment Pipeline",
    ))

    try:
        outputs = asyncio.run(run_pipeline(config))
        console.print("\n[bold green]Pipeline complete![/bold green]")
        table = Table(title="Output Files")
        table.add_column("Type", style="cyan")
        table.add_column("Path", style="green")
        for t, p in outputs.items():
            table.add_row(t, str(p))
        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@enrich.command("score")
@click.option(
    "--output-dir", "-d", "output_dir", type=click.Path(exists=True, path_type=Path),
    default=Path("./output"), help="Enrichment output directory to score",
)
@click.option("--handle", "-h", "handles", multiple=True, help="Specific handles to score (repeatable)")
@click.option("--verify/--no-verify", default=False, help="Run LLM fact-checking (requires API key)")
@click.option("--json-output", "json_out", is_flag=True, help="Output as JSON instead of table")
@click.option("--verbose", is_flag=True)
def score(output_dir, handles, verify, json_out, verbose):
    """Score enrichment quality across 5 axes.

    Reads merch_output.json and facts.json from the output directory and
    computes a composite quality score (0-100) for each product.

    Axes: factual fidelity, structural compliance, length targets,
    anti-hype compliance, and fact coverage.
    """
    import json as json_mod

    setup_logging(verbose)
    from lookout.enrich.scorer import AxisScore, score_output_dir

    handle_list = list(handles) if handles else None
    verifications: dict | None = None

    if verify:
        console.print("[dim]Running LLM fact-checking (this uses API credits)...[/dim]")
        from lookout.enrich.scorer import load_artifacts
        from lookout.enrich.llm import get_llm_client

        llm = get_llm_client()

        # Determine handles to verify
        if handle_list is None:
            handle_list = [
                d.name for d in sorted(output_dir.iterdir())
                if d.is_dir() and (d / "merch_output.json").exists()
            ]

        verifications = {}
        for h in handle_list:
            merch, facts = load_artifacts(output_dir, h)
            if merch and merch.body_html and facts:
                console.print(f"  [dim]Verifying {h}...[/dim]")
                result = asyncio.run(llm.verify_description(facts.model_dump(), merch.body_html))
                verifications[h] = result

    scores = score_output_dir(output_dir, handle_list, verifications)

    if not scores:
        console.print("[yellow]No scoreable products found in output directory.[/yellow]")
        return

    if json_out:
        console.print(json_mod.dumps([s.summary_dict() for s in scores], indent=2))
        return

    # Summary table
    table = Table(title="Enrichment Quality Scores")
    table.add_column("Handle", style="cyan", max_width=30)
    table.add_column("Total", style="bold green", justify="right")
    table.add_column("Fidelity\n(0-30)", justify="right")
    table.add_column("Structure\n(0-25)", justify="right")
    table.add_column("Length\n(0-15)", justify="right")
    table.add_column("Anti-hype\n(0-15)", justify="right")
    table.add_column("Coverage\n(0-15)", justify="right")

    for s in sorted(scores, key=lambda x: x.total, reverse=True):
        a = s.axes
        table.add_row(
            s.handle,
            f"{s.total}/{s.max_total}",
            str(a.get("factual_fidelity", AxisScore("", 0, 30)).score),
            str(a.get("structural_compliance", AxisScore("", 0, 25)).score),
            str(a.get("length_targets", AxisScore("", 0, 15)).score),
            str(a.get("anti_hype", AxisScore("", 0, 15)).score),
            str(a.get("coverage", AxisScore("", 0, 15)).score),
        )

    console.print(table)

    # Aggregate stats
    avg_total = sum(s.total for s in scores) / len(scores)
    avg_pct = sum(s.pct for s in scores) / len(scores)
    console.print(f"\n[bold]Average:[/bold] {avg_total:.1f}/{scores[0].max_total} ({avg_pct:.1f}%)")
    console.print(f"[bold]Products scored:[/bold] {len(scores)}")

    # Per-axis averages
    if verbose:
        console.print("\n[bold]Per-axis averages:[/bold]")
        axis_names = ["factual_fidelity", "structural_compliance", "length_targets", "anti_hype", "coverage"]
        for name in axis_names:
            vals = [s.axes[name].score for s in scores if name in s.axes]
            maxes = [s.axes[name].max_score for s in scores if name in s.axes]
            if vals:
                avg = sum(vals) / len(vals)
                max_val = maxes[0]
                console.print(f"  {name}: {avg:.1f}/{max_val}")


@enrich.command("optimize")
@click.option(
    "--test-dir", "-t", "test_dir", type=click.Path(exists=True, path_type=Path),
    default=Path("./test_set/combined"), help="Test set directory with cached artifacts",
)
@click.option(
    "--prompt", "-p", "prompt_path", type=click.Path(exists=True, path_type=Path),
    default=Path("./lookout/enrich/prompts/generate_body_html.prompt"),
    help="Prompt file to optimize",
)
@click.option(
    "--log-dir", "-l", "log_dir", type=click.Path(path_type=Path),
    default=Path("./test_set/optimize_log"), help="Directory for iteration logs",
)
@click.option("--max-iterations", "-n", default=5, help="Maximum optimization iterations")
@click.option("--verbose", is_flag=True)
def optimize(test_dir, prompt_path, log_dir, max_iterations, verbose):
    """Run the Karpathy Loop to optimize the enrichment prompt.

    Iteratively modifies the prompt, regenerates descriptions for the
    test set using cached facts (no scraping), scores each iteration,
    and keeps the best-performing prompt.
    """
    setup_logging(verbose)
    from lookout.enrich.optimize import run_optimization_loop

    console.print(Panel(
        f"[bold]Karpathy Loop: Prompt Optimization[/bold]\n"
        f"Test set: {test_dir}\n"
        f"Prompt: {prompt_path}\n"
        f"Max iterations: {max_iterations}",
        title="Optimize",
    ))

    try:
        history = asyncio.run(run_optimization_loop(
            test_dir=test_dir,
            prompt_path=prompt_path,
            log_dir=log_dir,
            max_iterations=max_iterations,
        ))
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    # Display results
    table = Table(title="Optimization History")
    table.add_column("Iter", justify="right")
    table.add_column("Avg Score", justify="right", style="bold")
    table.add_column("Structure\n(0-25)", justify="right")
    table.add_column("Length\n(0-15)", justify="right")
    table.add_column("Anti-hype\n(0-15)", justify="right")
    table.add_column("Coverage\n(0-15)", justify="right")
    table.add_column("Delta", justify="right")

    for i, r in enumerate(history):
        delta = ""
        if i > 0:
            d = r.avg_score - history[i - 1].avg_score
            color = "green" if d > 0 else "red" if d < 0 else "dim"
            delta = f"[{color}]{d:+.1f}[/{color}]"

        table.add_row(
            str(r.iteration),
            f"{r.avg_score:.1f}/70",
            f"{r.per_axis.get('structural_compliance', 0):.1f}",
            f"{r.per_axis.get('length_targets', 0):.1f}",
            f"{r.per_axis.get('anti_hype', 0):.1f}",
            f"{r.per_axis.get('coverage', 0):.1f}",
            delta,
        )

    console.print(table)

    best = max(history, key=lambda r: r.avg_score)
    baseline = history[0].avg_score
    console.print(f"\n[bold]Best:[/bold] iteration {best.iteration} ({best.avg_score:.1f}/70)")
    console.print(f"[bold]Improvement:[/bold] {best.avg_score - baseline:+.1f} from baseline")
    console.print(f"[bold]Logs:[/bold] {log_dir}")


@enrich.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--verbose", is_flag=True)
def validate(input_path, verbose):
    """Validate an input CSV file."""
    setup_logging(verbose)
    from lookout.enrich.io import parse_input_csv

    valid = 0
    vendors = {}
    for row in parse_input_csv(input_path):
        valid += 1
        vendors[row.vendor] = vendors.get(row.vendor, 0) + 1

    table = Table(title="CSV Validation")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Valid Rows", str(valid))
    console.print(table)

    if vendors:
        vt = Table(title="Vendors")
        vt.add_column("Vendor", style="cyan")
        vt.add_column("Count", style="green")
        for v, c in sorted(vendors.items(), key=lambda x: -x[1]):
            vt.add_row(v, str(c))
        console.print(vt)


# ---------------------------------------------------------------------------
# rank
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--collection", default=None, help="Collection handle")
@click.option("--vendor", "-v", default=None, help="Filter by vendor")
@click.option("--product-type", default=None, help="Filter by product type")
@click.option(
    "--out", "-o", "output_path", type=click.Path(path_type=Path), default=None,
    help="Export rankings CSV",
)
@click.option("--limit", type=int, default=200, help="Max products to rank")
@click.option("--verbose", is_flag=True)
def rank(collection, vendor, product_type, output_path, limit, verbose):
    """Rank products for collection merchandising."""
    setup_logging(verbose)
    import csv

    from lookout.ranking.ranker import CollectionRanker
    from lookout.store import LookoutStore

    store = LookoutStore()
    ranker = CollectionRanker(store)
    result = ranker.rank(
        collection=collection, vendor=vendor, product_type=product_type, limit=limit,
    )

    if output_path:
        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Rank", "Handle", "Title", "Vendor", "Score",
                "Weekly Units", "Margin %", "Inventory", "WOS",
            ])
            for p in result.ranked:
                writer.writerow([
                    p.rank, p.handle, p.title, p.vendor, f"{p.total_score:.3f}",
                    f"{p.weekly_units:.1f}", f"{p.margin_pct:.1f}",
                    p.total_inventory, f"{p.weeks_of_supply:.1f}",
                ])
        console.print(f"Rankings written to [green]{output_path}[/green]")
    else:
        table = Table(title=f"Rankings: {result.collection_name}")
        table.add_column("Rank", style="dim")
        table.add_column("Product")
        table.add_column("Vendor", style="cyan")
        table.add_column("Score", style="green")
        table.add_column("Units/wk")
        table.add_column("Inv")
        for p in result.ranked[:50]:
            table.add_row(
                str(p.rank), p.title[:40], p.vendor,
                f"{p.total_score:.2f}", f"{p.weekly_units:.1f}", str(p.total_inventory),
            )
        console.print(table)


# ---------------------------------------------------------------------------
# vendors
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--vendors", "vendors_path", type=click.Path(path_type=Path), default=None,
    help="Path to vendors.yaml",
)
def vendors(vendors_path):
    """List configured scraping vendors."""
    from lookout.enrich.utils.config import load_vendors_config

    vendors_path = find_vendors_yaml(vendors_path)
    if not vendors_path or not vendors_path.exists():
        console.print("[red]Error:[/red] vendors.yaml not found")
        sys.exit(1)

    config = load_vendors_config(vendors_path)
    table = Table(title="Configured Vendors")
    table.add_column("Vendor", style="cyan")
    table.add_column("Domain", style="green")
    table.add_column("Playwright", style="yellow")
    for name, vendor in config.vendors.items():
        table.add_row(name, vendor.domain, "Yes" if vendor.use_playwright else "No")
    console.print(table)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------


@cli.group()
def output():
    """Generate output files for Shopify import."""


@output.command("matrixify-images")
@click.option("--vendor", "-v", default=None, help="Filter by vendor")
@click.option("--dry-run", is_flag=True, help="Don't write to DB")
@click.option("--out", "-o", "output_path", type=click.Path(path_type=Path), default=None)
@click.option("--verbose", is_flag=True)
def matrixify_images(vendor, dry_run, output_path, verbose):
    """Catalog-based image enrichment -> Matrixify CSV."""
    setup_logging(verbose)
    from lookout.output.matrixify import ImageEnricher, MatrixifyExporter
    from lookout.store import LookoutStore

    store = LookoutStore()
    enricher = ImageEnricher(store)
    result = enricher.enrich(vendor=vendor, dry_run=dry_run)

    console.print(f"Propagated from Shopify: {result.propagated_from_shopify}")
    console.print(f"Matched from catalog: {result.matched_from_catalog}")
    console.print(f"Matched from style map: {result.matched_from_style_map}")
    console.print(f"Still missing: {result.still_missing}")

    if output_path and result.assignments:
        csv_content = MatrixifyExporter.export_enriched_images(result.assignments)
        output_path.write_text(csv_content)
        console.print(f"Written to [green]{output_path}[/green]")


@output.command("alt-text")
@click.option(
    "--out", "-o", "output_path", type=click.Path(path_type=Path),
    default=Path("alt_text.xlsx"),
)
@click.option("--verbose", is_flag=True)
def alt_text(output_path, verbose):
    """Generate WCAG alt text XLSX."""
    setup_logging(verbose)
    from lookout.output.alt_text import generate_alt_text_xlsx
    from lookout.store import LookoutStore

    store = LookoutStore()
    stats = generate_alt_text_xlsx(output_path, store)
    console.print(f"Generated alt text for {stats['products']} products, {stats['images']} images")


@output.command("google-shopping")
@click.option(
    "--out", "-o", "output_path", type=click.Path(path_type=Path),
    default=Path("google_shopping.xlsx"),
)
@click.option("--verbose", is_flag=True)
def google_shopping(output_path, verbose):
    """Generate Google Shopping metafields + SEO XLSX."""
    setup_logging(verbose)
    from lookout.output.google_shopping import generate_google_shopping
    from lookout.store import LookoutStore

    store = LookoutStore()
    stats = generate_google_shopping(output_path, store)
    console.print(
        f"Generated for {stats['total_products']} products, {stats['total_variants']} variants"
    )


@output.command("weights")
@click.option(
    "--out", "-o", "output_path", type=click.Path(path_type=Path),
    default=Path("weights.xlsx"),
)
@click.option("--verbose", is_flag=True)
def weights(output_path, verbose):
    """Generate weight corrections XLSX."""
    setup_logging(verbose)
    from lookout.output.google_shopping import generate_weights
    from lookout.store import LookoutStore

    store = LookoutStore()
    stats = generate_weights(output_path, store)
    console.print(f"Updated {stats['variants_updated']} variants")


@output.command("weight-audit")
@click.option(
    "--out", "-o", "output_path", type=click.Path(path_type=Path),
    default=Path("weight_audit.csv"),
)
@click.option("--verbose", is_flag=True)
def weight_audit(output_path, verbose):
    """Generate weight audit CSV for review."""
    setup_logging(verbose)
    from lookout.output.google_shopping import generate_weight_audit
    from lookout.store import LookoutStore

    store = LookoutStore()
    stats = generate_weight_audit(output_path, store)
    console.print(f"Generated {stats['rows']} rows for review")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
