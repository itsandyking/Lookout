"""Unified CLI for Lookout — merchandising command center."""

import asyncio
import logging
import sys
from pathlib import Path

import click
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
@click.option("--verbose", is_flag=True)
def audit(vendor, output_path, verbose):
    """Run content audit — find products with gaps."""
    setup_logging(verbose)
    from lookout.audit.auditor import ContentAuditor
    from lookout.store import LookoutStore

    try:
        store = LookoutStore()
    except Exception as e:
        console.print(f"[red]Error connecting to database:[/red] {e}")
        sys.exit(1)

    auditor = ContentAuditor(store)
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
@click.option("--verbose", is_flag=True)
def run(input_path, vendor, output_dir, vendors_path, concurrency, max_rows, force, dry_run, verbose):
    """Run the enrichment pipeline."""
    setup_logging(verbose)
    from lookout.enrich.pipeline import PipelineConfig, run_pipeline

    vendors_path = find_vendors_yaml(vendors_path)
    if not vendors_path:
        console.print("[red]Error:[/red] vendors.yaml not found")
        sys.exit(1)

    # If no input CSV, run audit internally to get priorities
    if input_path is None and vendor:
        console.print(f"[dim]Running internal audit for vendor: {vendor}[/dim]")
        import tempfile

        from lookout.audit.auditor import ContentAuditor
        from lookout.store import LookoutStore

        store = LookoutStore()
        auditor = ContentAuditor(store)
        result = auditor.audit(vendor=vendor)

        if not result.priority_items:
            console.print("[yellow]No products with gaps found.[/yellow]")
            return

        csv_bytes = result.to_priority_csv()
        tmp = Path(tempfile.mktemp(suffix=".csv"))
        tmp.write_bytes(csv_bytes)
        input_path = tmp
        console.print(f"[dim]Found {len(result.priority_items)} products with gaps[/dim]")

    if input_path is None:
        console.print("[red]Error:[/red] Provide --input CSV or --vendor for internal audit")
        sys.exit(1)

    config = PipelineConfig(
        input_path=input_path,
        output_dir=output_dir,
        vendors_path=vendors_path,
        concurrency=concurrency,
        max_rows=max_rows,
        force=force,
        dry_run=dry_run,
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


@enrich.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--verbose", is_flag=True)
def validate(input_path, verbose):
    """Validate an input CSV file."""
    setup_logging(verbose)
    from lookout.enrich.csv_parser import parse_input_csv

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
