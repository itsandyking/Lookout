"""
Command-line interface for merchfill.

Provides the main entry point for running the pipeline.
"""

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from .pipeline import PipelineConfig, run_pipeline

console = Console()


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with rich output."""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                show_path=False,
            )
        ],
    )

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


@click.group()
@click.version_option(version="0.1.0")
def cli() -> None:
    """
    Merchfill - Automated merchandising pipeline for Shopify.

    Fill missing product content using vendor websites.
    """
    pass


@cli.command()
@click.option(
    "--input",
    "-i",
    "input_path",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to input CSV (merchandising_priority.csv format)",
)
@click.option(
    "--out",
    "-o",
    "output_dir",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory for generated files",
)
@click.option(
    "--vendors",
    "-v",
    "vendors_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to vendors.yaml config (default: ./vendors.yaml)",
)
@click.option(
    "--shopify-export",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Optional Shopify product export CSV for variant rows",
)
@click.option(
    "--concurrency",
    "-c",
    type=int,
    default=5,
    help="Maximum concurrent requests (default: 5)",
)
@click.option(
    "--max-rows",
    "-n",
    type=int,
    default=None,
    help="Maximum rows to process (for testing)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Force re-processing even if cached",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Process but don't write shopify_update.csv",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Enable verbose logging",
)
def run(
    input_path: Path,
    output_dir: Path,
    vendors_path: Path | None,
    shopify_export: Path | None,
    concurrency: int,
    max_rows: int | None,
    force: bool,
    dry_run: bool,
    verbose: bool,
) -> None:
    """
    Run the merchandising pipeline.

    Reads product data from INPUT CSV, resolves vendor URLs, scrapes content,
    and generates Shopify-compatible output files.

    Example:
        merchfill run --input merchandising_priority.csv --out ./output
    """
    setup_logging(verbose)

    # Find vendors.yaml if not specified
    if vendors_path is None:
        candidates = [
            Path.cwd() / "vendors.yaml",
            Path(__file__).parent.parent / "vendors.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                vendors_path = candidate
                break

        if vendors_path is None:
            console.print(
                "[red]Error:[/red] vendors.yaml not found. "
                "Specify with --vendors or place in current directory."
            )
            sys.exit(1)

    # Show configuration
    console.print(
        Panel(
            f"[bold]Input:[/bold] {input_path}\n"
            f"[bold]Output:[/bold] {output_dir}\n"
            f"[bold]Vendors:[/bold] {vendors_path}\n"
            f"[bold]Concurrency:[/bold] {concurrency}\n"
            f"[bold]Force:[/bold] {force}\n"
            f"[bold]Dry Run:[/bold] {dry_run}",
            title="Pipeline Configuration",
        )
    )

    # Create config
    config = PipelineConfig(
        input_path=input_path,
        output_dir=output_dir,
        vendors_path=vendors_path,
        shopify_export_path=shopify_export,
        concurrency=concurrency,
        max_rows=max_rows,
        force=force,
        dry_run=dry_run,
    )

    # Run pipeline
    try:
        outputs = asyncio.run(run_pipeline(config))

        # Show results
        console.print("\n[bold green]Pipeline complete![/bold green]\n")

        table = Table(title="Output Files")
        table.add_column("Type", style="cyan")
        table.add_column("Path", style="green")

        for output_type, path in outputs.items():
            table.add_row(output_type, str(path))

        console.print(table)

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        if verbose:
            console.print_exception()
        sys.exit(1)


@cli.command()
@click.option(
    "--vendors",
    "-v",
    "vendors_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to vendors.yaml config",
)
def list_vendors(vendors_path: Path | None) -> None:
    """List configured vendors."""
    from .utils import load_vendors_config

    # Find vendors.yaml
    if vendors_path is None:
        candidates = [
            Path.cwd() / "vendors.yaml",
            Path(__file__).parent.parent / "vendors.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                vendors_path = candidate
                break

    if vendors_path is None or not vendors_path.exists():
        console.print("[red]Error:[/red] vendors.yaml not found")
        sys.exit(1)

    config = load_vendors_config(vendors_path)

    table = Table(title="Configured Vendors")
    table.add_column("Vendor", style="cyan")
    table.add_column("Domain", style="green")
    table.add_column("Playwright", style="yellow")
    table.add_column("Blocked Paths", style="dim")

    for name, vendor in config.vendors.items():
        table.add_row(
            name,
            vendor.domain,
            "Yes" if vendor.use_playwright else "No",
            ", ".join(vendor.blocked_paths[:3]) + ("..." if len(vendor.blocked_paths) > 3 else ""),
        )

    console.print(table)


@cli.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
def validate_csv(input_path: Path) -> None:
    """Validate an input CSV file."""
    from .csv_parser import parse_input_csv

    console.print(f"Validating: {input_path}\n")

    valid_count = 0
    vendors: dict[str, int] = {}
    gaps: dict[str, int] = {
        "description": 0,
        "image": 0,
        "variant_image": 0,
    }

    for row in parse_input_csv(input_path):
        valid_count += 1
        vendors[row.vendor] = vendors.get(row.vendor, 0) + 1

        if row.needs_description:
            gaps["description"] += 1
        if row.needs_images:
            gaps["image"] += 1
        if row.needs_variant_images:
            gaps["variant_image"] += 1

    # Summary table
    table = Table(title="CSV Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Valid Rows", str(valid_count))
    table.add_row("Missing Descriptions", str(gaps["description"]))
    table.add_row("Missing Images", str(gaps["image"]))
    table.add_row("Missing Variant Images", str(gaps["variant_image"]))

    console.print(table)

    # Vendors table
    vendor_table = Table(title="Vendors")
    vendor_table.add_column("Vendor", style="cyan")
    vendor_table.add_column("Products", style="green")

    for vendor, count in sorted(vendors.items(), key=lambda x: -x[1]):
        vendor_table.add_row(vendor, str(count))

    console.print(vendor_table)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
