"""Unified CLI for Lookout — merchandising command center."""

import asyncio
import logging
import subprocess
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


FIRECRAWL_SRC_DIR = Path.home() / "firecrawl-src"


@cli.group()
def infra():
    """Manage infrastructure services (Firecrawl, etc.)."""


@infra.command()
def up():
    """Start Firecrawl services via Docker Compose."""
    compose_file = FIRECRAWL_SRC_DIR / "docker-compose.yaml"
    if not compose_file.exists():
        console.print(
            f"[red]Firecrawl source not found at {FIRECRAWL_SRC_DIR}[/red]\n"
            "Clone it first: git clone https://github.com/firecrawl/firecrawl.git ~/firecrawl-src"
        )
        raise SystemExit(1)
    console.print("[bold]Starting Firecrawl...[/bold]")
    result = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=FIRECRAWL_SRC_DIR,
    )
    if result.returncode == 0:
        console.print("[green]Firecrawl running at http://localhost:3002[/green]")
    raise SystemExit(result.returncode)


@infra.command()
def down():
    """Stop Firecrawl services."""
    console.print("[bold]Stopping Firecrawl...[/bold]")
    result = subprocess.run(
        ["docker", "compose", "down"],
        cwd=FIRECRAWL_SRC_DIR,
    )
    raise SystemExit(result.returncode)


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
@click.option("--online/--no-online", default=False, help="Enrich with online opportunity signals (sessions, conversion)")
@click.option("--gmc/--no-gmc", default=False, help="Enrich with Google Merchant Center signals (clicks, impressions, disapprovals)")
@click.option("--lookback", default=90, help="Days to look back for online/GMC signals (default 90)")
@click.option("--verbose", is_flag=True)
def audit(vendor, output_path, include_house_brands, online, gmc, lookback, verbose):
    """Run content audit — find products with gaps."""
    setup_logging(verbose)
    from lookout.audit.auditor import ContentAuditor
    from lookout.store import LookoutStore

    try:
        store = LookoutStore()
    except Exception as e:
        console.print(f"[red]Error connecting to database:[/red] {e}")
        sys.exit(1)

    online_signals = {}
    if online:
        console.print("[dim]Fetching online signals from Shopify analytics...[/dim]")
        try:
            from tvr.mcp_report.shopify_api import ShopifyQLClient
            from lookout.audit.online_signals import fetch_online_signals

            # Build title→handle mapping for joining session data
            all_products = store.list_products(status="active")
            title_to_handle = {
                p.get("title", ""): p.get("handle", "")
                for p in all_products if p.get("title") and p.get("handle")
            }

            client = ShopifyQLClient.from_config()
            online_signals = asyncio.run(fetch_online_signals(
                client, title_to_handle=title_to_handle, lookback_days=lookback,
            ))
            console.print(f"[dim]Got signals for {len(online_signals)} products[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not fetch online signals: {e}[/yellow]")
            console.print("[dim]Falling back to inventory-only priority scoring[/dim]")

    gmc_signals = {}
    if gmc:
        console.print("[dim]Fetching Google Merchant Center signals...[/dim]")
        try:
            from lookout.audit.gmc_signals import fetch_all_gmc_signals

            gmc_signals = fetch_all_gmc_signals(lookback_days=lookback)
            console.print(f"[dim]Got GMC signals for {len(gmc_signals)} products[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not fetch GMC signals: {e}[/yellow]")
            console.print("[dim]Falling back to non-GMC priority scoring[/dim]")

    auditor = ContentAuditor(
        store,
        exclude_house_brands=not include_house_brands,
        online_signals=online_signals,
        gmc_signals=gmc_signals,
    )
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


@cli.command("audit-snapshot")
@click.option("--online/--no-online", default=True, help="Enrich with online opportunity signals")
@click.option("--gmc/--no-gmc", default=False, help="Enrich with Google Merchant Center signals")
@click.option("--lookback", default=90, help="Days to look back for online/GMC signals")
@click.option("--out", "-o", required=True, type=click.Path(path_type=Path), help="Output snapshot JSON path")
@click.option("--include-house-brands", is_flag=True, help="Include house brands in audit")
@click.option("--verbose", is_flag=True)
def audit_snapshot(online, gmc, lookback, out, include_house_brands, verbose):
    """Run content audit and save a snapshot for weight optimization."""
    setup_logging(verbose)
    from lookout.audit.auditor import ContentAuditor
    from lookout.audit.weight_optimizer import save_snapshot
    from lookout.store import LookoutStore

    try:
        store = LookoutStore()
    except Exception as e:
        console.print(f"[red]Error connecting to database:[/red] {e}")
        sys.exit(1)

    online_signals = {}
    if online:
        console.print("[dim]Fetching online signals from Shopify analytics...[/dim]")
        try:
            from tvr.mcp_report.shopify_api import ShopifyQLClient
            from lookout.audit.online_signals import fetch_online_signals

            all_products = store.list_products(status="active")
            title_to_handle = {
                p.get("title", ""): p.get("handle", "")
                for p in all_products if p.get("title") and p.get("handle")
            }

            client = ShopifyQLClient.from_config()
            online_signals = asyncio.run(fetch_online_signals(
                client, title_to_handle=title_to_handle, lookback_days=lookback,
            ))
            console.print(f"[dim]Got signals for {len(online_signals)} products[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not fetch online signals: {e}[/yellow]")

    gmc_signals = {}
    if gmc:
        console.print("[dim]Fetching Google Merchant Center signals...[/dim]")
        try:
            from lookout.audit.gmc_signals import fetch_all_gmc_signals

            gmc_signals = fetch_all_gmc_signals(lookback_days=lookback)
            console.print(f"[dim]Got GMC signals for {len(gmc_signals)} products[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not fetch GMC signals: {e}[/yellow]")

    auditor = ContentAuditor(
        store,
        exclude_house_brands=not include_house_brands,
        online_signals=online_signals,
        gmc_signals=gmc_signals,
    )
    result = auditor.audit()
    summary = result.summary()

    console.print(f"Audited {summary['total_products']} products, {summary['products_with_gaps']} with gaps")

    save_snapshot(result.scores, Path(out))
    console.print(f"Snapshot saved to [green]{out}[/green]")


@cli.command("audit-optimize")
@click.option("--snapshot", "-s", required=True, type=click.Path(exists=True, path_type=Path), help="Snapshot JSON from audit-snapshot")
@click.option("--top-n", default=50, help="Top-N products to optimize for (default 50)")
@click.option("--log-dir", "-l", default=Path("./audit_optimize_log"), type=click.Path(path_type=Path), help="Directory for optimization logs")
@click.option("--max-iterations", "-n", default=200, help="Max iterations per restart")
@click.option("--verbose", is_flag=True)
def audit_optimize(snapshot, top_n, log_dir, max_iterations, verbose):
    """Optimize audit priority weights for coverage efficiency.

    Tunes weights to maximize how well the top-N products cover:
    variant leverage, vendor clustering, inventory value, and online traffic.
    No manual ranking needed — the metric is computed from the data.
    """
    setup_logging(verbose)
    from lookout.audit.weight_config import PriorityWeights
    from lookout.audit.weight_optimizer import load_snapshot, run_weight_optimization

    scores = load_snapshot(Path(snapshot))

    console.print(Panel(
        f"[bold]Audit Weight Optimization[/bold]\n"
        f"Snapshot: {snapshot} ({len(scores)} products)\n"
        f"Optimizing top-{top_n} coverage efficiency\n"
        f"Max iterations: {max_iterations}",
        title="Optimize",
    ))

    result = run_weight_optimization(
        scores=scores,
        log_dir=Path(log_dir),
        top_n=top_n,
        max_iterations=max_iterations,
    )

    # Compare default vs optimized
    default = PriorityWeights()
    best: PriorityWeights = result["best_weights"]

    table = Table(title="Weight Comparison: Default vs Optimized")
    table.add_column("Parameter", style="cyan")
    table.add_column("Default", justify="right")
    table.add_column("Optimized", justify="right", style="green")
    table.add_column("Change", justify="right")

    for key in default.to_dict():
        d_val = getattr(default, key)
        b_val = getattr(best, key)
        if isinstance(d_val, float):
            change = b_val - d_val
            color = "green" if change > 0 else "red" if change < 0 else "dim"
            table.add_row(key, f"{d_val:.3f}", f"{b_val:.3f}", f"[{color}]{change:+.3f}[/{color}]")
        else:
            changed = " *" if d_val != b_val else ""
            table.add_row(key, str(d_val), str(b_val), changed)

    console.print(table)

    # Coverage breakdown comparison
    bl = result["baseline_breakdown"]
    bt = result["best_breakdown"]

    btable = Table(title="Coverage Efficiency Breakdown")
    btable.add_column("Component", style="cyan")
    btable.add_column("Baseline", justify="right")
    btable.add_column("Optimized", justify="right", style="green")

    btable.add_row("Variant leverage (0.30)", f"{bl['variant_leverage']:.1%}", f"{bt['variant_leverage']:.1%}")
    btable.add_row("Vendor concentration (0.20)", f"{bl['vendor_concentration']:.1%}", f"{bt['vendor_concentration']:.1%}")
    btable.add_row("Inventory coverage (0.25)", f"{bl['inventory_coverage']:.1%}", f"{bt['inventory_coverage']:.1%}")
    btable.add_row("Traffic alignment (0.25)", f"{bl['traffic_alignment']:.1%}", f"{bt['traffic_alignment']:.1%}")
    btable.add_row("[bold]Composite[/bold]", f"[bold]{bl['composite']:.1%}[/bold]", f"[bold green]{bt['composite']:.1%}[/bold green]")

    console.print(btable)

    if bt.get("top_vendors"):
        console.print(f"\n[bold]Top vendors in optimized batch:[/bold]")
        for vendor, count in bt["top_vendors"]:
            console.print(f"  {vendor}: {count} products")

    console.print(f"\n[bold]Improvement:[/bold] {result['best_efficiency'] - result['baseline_efficiency']:+.4f}")
    console.print(f"[bold]Logs:[/bold] {log_dir}")


@cli.command("shipping")
@click.option(
    "--out", "-o", "output_path", type=click.Path(path_type=Path), default=None,
    help="Export shipping audit CSV",
)
@click.option("--verbose", is_flag=True)
def shipping(output_path, verbose):
    """Audit shipping weights and cost ratios.

    Finds zero-weight products (broken checkout), weight mismatches
    (wrong shipping quotes), and products where shipping cost is a
    high percentage of product price (conversion blocker).
    """
    setup_logging(verbose)
    from lookout.audit.shipping import run_shipping_audit, export_shipping_audit_csv
    from lookout.store import LookoutStore

    try:
        store = LookoutStore()
    except Exception as e:
        console.print(f"[red]Error connecting to database:[/red] {e}")
        sys.exit(1)

    issues = run_shipping_audit(store)

    # Summary
    critical = [i for i in issues if i.severity == "critical"]
    warnings = [i for i in issues if i.severity == "warning"]
    info = [i for i in issues if i.severity == "info"]

    zero_weight = [i for i in issues if i.issue_type == "zero_weight"]
    mismatches = [i for i in issues if i.issue_type == "weight_mismatch"]
    high_ratio = [i for i in issues if i.issue_type == "high_shipping_ratio"]

    table = Table(title="Shipping Audit Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("[red]Critical[/red] (zero weight)", str(len(zero_weight)))
    table.add_row("[yellow]Warning[/yellow] (weight mismatch + high ratio)", str(len(warnings)))
    table.add_row("Info (moderate ratio)", str(len(info)))
    table.add_row("Total issues", str(len(issues)))
    console.print(table)

    # Show critical issues
    if zero_weight:
        console.print(f"\n[red bold]Zero-weight products ({len(zero_weight)}):[/red bold]")
        zt = Table()
        zt.add_column("Handle", style="red", max_width=35)
        zt.add_column("Type")
        zt.add_column("Vendor")
        zt.add_column("Est. Weight")
        for i in zero_weight[:20]:
            zt.add_row(
                i.handle,
                i.product_type,
                i.vendor,
                f"{i.estimated_weight_g}g" if i.estimated_weight_g else "unknown",
            )
        console.print(zt)

    # Show high shipping ratio
    if high_ratio:
        console.print(f"\n[yellow bold]High shipping-to-price ratio ({len(high_ratio)}):[/yellow bold]")
        ht = Table()
        ht.add_column("Handle", max_width=35)
        ht.add_column("Price", justify="right")
        ht.add_column("Est. Ship", justify="right")
        ht.add_column("Ratio", justify="right")
        ht.add_column("Type")
        for i in sorted(high_ratio, key=lambda x: -x.shipping_to_price_pct)[:20]:
            color = "red" if i.shipping_to_price_pct > 40 else "yellow"
            ht.add_row(
                i.handle,
                f"${i.price:.0f}",
                f"${i.estimated_shipping:.0f}",
                f"[{color}]{i.shipping_to_price_pct:.0f}%[/{color}]",
                i.product_type,
            )
        console.print(ht)

    if output_path:
        export_shipping_audit_csv(issues, output_path)
        console.print(f"\nShipping audit CSV written to [green]{output_path}[/green]")


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
@click.option("--handle", "-h", "handles", multiple=True, help="Specific product handles to enrich (repeatable)")
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
def run(input_path, vendor, handles, output_dir, vendors_path, concurrency, max_rows, force, dry_run, verify, only_mode, verbose):
    """Run the enrichment pipeline."""
    setup_logging(verbose)
    from lookout.enrich.pipeline import PipelineConfig, run_pipeline

    vendors_path = find_vendors_yaml(vendors_path)
    if not vendors_path:
        console.print("[red]Error:[/red] vendors.yaml not found")
        sys.exit(1)

    # If specific handles provided, build InputRows directly from Shopify API
    enriched_rows = None
    if handles:
        from lookout.audit.auditor import ContentAuditor
        from lookout.store import LookoutStore

        store = LookoutStore()
        auditor = ContentAuditor(store, exclude_house_brands=False)
        enriched_rows = []
        for handle in handles:
            product = store.get_product(handle)
            if not product:
                console.print(f"[yellow]Handle not found: {handle}[/yellow]")
                continue
            # Run audit on just this product to get gap analysis + variant data
            score = auditor._score_product(product)
            from lookout.audit.models import AuditResult
            mini_result = AuditResult(scores=[score], vendor=product.get("vendor", ""))
            # Use all_items (not priority_items) so handle mode works even for gapless products
            rows = mini_result.to_input_rows(store=store, include_all=True)
            enriched_rows.extend(rows)

        if not enriched_rows:
            console.print("[red]No valid handles found.[/red]")
            return
        console.print(f"[dim]Enriching {len(enriched_rows)} products by handle[/dim]")

    # If no input CSV and no handles, run audit internally to get priorities with rich variant data
    elif input_path is None:
        label = f"vendor: {vendor}" if vendor else "all vendors"
        console.print(f"[dim]Running internal audit for {label}[/dim]")

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
        console.print("[red]Error:[/red] Provide --input CSV or run with internal audit")
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


@enrich.command("score-facts")
@click.option(
    "--test-dir", "-t", type=click.Path(exists=True, path_type=Path),
    default=Path("./test_set/combined"), help="Directory with extracted facts",
)
@click.option("--verbose", is_flag=True)
def score_facts_cmd(test_dir, verbose):
    """Score extracted fact quality across 4 axes.

    Reads extracted_facts.json from each handle subdirectory and
    computes a composite quality score (0-100) measuring how useful
    the raw facts are before any LLM generation.

    Axes: content signal, field completeness, specificity, deduplication.
    """
    setup_logging(verbose)
    from lookout.enrich.fact_scorer import AxisScore, score_facts_dir

    scores = score_facts_dir(test_dir)

    if not scores:
        console.print("[yellow]No scoreable facts found in directory.[/yellow]")
        return

    # Summary table
    table = Table(title="Extracted Facts Quality Scores")
    table.add_column("Handle", style="cyan", max_width=40)
    table.add_column("Total", style="bold green", justify="right")
    table.add_column("Signal\n(0-30)", justify="right")
    table.add_column("Complete\n(0-25)", justify="right")
    table.add_column("Specific\n(0-25)", justify="right")
    table.add_column("Dedup\n(0-20)", justify="right")

    for s in sorted(scores, key=lambda x: x.total, reverse=True):
        a = s.axes
        table.add_row(
            s.handle,
            f"{s.total}/{s.max_total}",
            str(a.get("content_signal", AxisScore("", 0, 30)).score),
            str(a.get("field_completeness", AxisScore("", 0, 25)).score),
            str(a.get("specificity", AxisScore("", 0, 25)).score),
            str(a.get("deduplication", AxisScore("", 0, 20)).score),
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
        axis_names = ["content_signal", "field_completeness", "specificity", "deduplication"]
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
@click.option(
    "--feedback-dir", "-f", "feedback_dir",
    type=click.Path(exists=True, path_type=Path), default=None,
    help="Directory with user feedback JSON files (rejections/edits feed the optimizer)",
)
@click.option("--verbose", is_flag=True)
def optimize(test_dir, prompt_path, log_dir, max_iterations, feedback_dir, verbose):
    """Run the Karpathy Loop to optimize the enrichment prompt.

    Iteratively modifies the prompt, regenerates descriptions for the
    test set using cached facts (no scraping), scores each iteration,
    and keeps the best-performing prompt.

    With --feedback-dir, user review dispositions (rejections with reasons,
    human-edited products) are injected into the meta-prompt so the optimizer
    learns from real user signal. Rejected/edited products are weighted 2x.
    """
    setup_logging(verbose)
    from lookout.enrich.optimize import run_optimization_loop

    feedback_info = f"\nFeedback: {feedback_dir}" if feedback_dir else ""
    console.print(Panel(
        f"[bold]Karpathy Loop: Prompt Optimization[/bold]\n"
        f"Test set: {test_dir}\n"
        f"Prompt: {prompt_path}\n"
        f"Max iterations: {max_iterations}{feedback_info}",
        title="Optimize",
    ))

    try:
        history = asyncio.run(run_optimization_loop(
            test_dir=test_dir,
            prompt_path=prompt_path,
            log_dir=log_dir,
            max_iterations=max_iterations,
            feedback_dir=feedback_dir,
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


# ---------------------------------------------------------------------------
# enrich review / apply / revert / feedback
# ---------------------------------------------------------------------------


@enrich.command("review")
@click.option("--run-dir", "-d", required=True, type=click.Path(exists=True, path_type=Path),
              help="Enrichment output directory to review")
@click.option("--out", "-o", type=click.Path(path_type=Path), default=None,
              help="Output HTML report path (default: {run-dir}/review.html)")
@click.option("--serve", is_flag=True, help="Start a local server (accessible from phone)")
@click.option("--port", "-p", type=int, default=8787, help="Server port (default: 8787)")
@click.option("--verbose", is_flag=True)
def review(run_dir, out, serve, port, verbose):
    """Generate a review report for enrichment output.

    Creates an HTML file with side-by-side current vs proposed descriptions.
    Open it in a browser, approve/reject each product, then save dispositions.

    Use --serve to start a local server accessible from your phone.
    """
    setup_logging(verbose)
    import json as json_mod
    from lookout.apply.models import ApplyRun, ProductChange
    from lookout.enrich.models import MerchOutput
    from lookout.review.report import generate_review_report
    from lookout.store import LookoutStore

    store = LookoutStore()
    run_dir = Path(run_dir)

    # Build changes from enrichment artifacts
    changes = []
    for handle_dir in sorted(run_dir.iterdir()):
        if not handle_dir.is_dir():
            continue
        merch_path = handle_dir / "merch_output.json"
        if not merch_path.exists():
            continue

        merch = MerchOutput(**json_mod.loads(merch_path.read_text()))

        # Get source URL from resolver output
        source_url = None
        resolver_path = handle_dir / "resolver.json"
        if resolver_path.exists():
            try:
                resolver_data = json_mod.loads(resolver_path.read_text())
                source_url = resolver_data.get("selected_url")
            except Exception:
                pass

        product = store.get_product(merch.handle)
        if not product:
            console.print(f"[yellow]Skipping {merch.handle}: not found in store[/yellow]")
            continue

        # Get image data from merch output
        new_images = [img.model_dump() if hasattr(img, 'model_dump') else img for img in (merch.images or [])]
        current_images = product.get("images", [])
        variant_image_map = dict(merch.variant_image_map) if merch.variant_image_map else None

        # Get variant labels for display
        variants = store.get_variants(product["id"])
        variant_labels = []
        for v in variants:
            parts = []
            for i in (1, 2, 3):
                name = v.get(f"option{i}_name", "")
                val = v.get(f"option{i}_value", "")
                if val:
                    parts.append(val)
            label = " / ".join(parts) if parts else v.get("sku", "")
            if label and label not in variant_labels:
                variant_labels.append(label)

        # Inventory and cost
        try:
            inv = store.get_inventory(product["id"])
            inventory_count = inv.get("total_quantity", 0)
            inventory_value = inv.get("total_cost", 0.0) or 0.0
        except Exception:
            inventory_count = 0
            inventory_value = 0.0

        # Missing fields check
        missing_fields = []
        if not product.get("product_type"):
            missing_fields.append("product_type")
        if not product.get("tags"):
            missing_fields.append("tags")

        changes.append(ProductChange(
            handle=merch.handle,
            product_id=product["id"],
            title=product.get("title", ""),
            vendor=product.get("vendor", ""),
            current_body_html=product.get("body_html", ""),
            new_body_html=merch.body_html,
            current_images=current_images,
            new_images=new_images,
            new_variant_image_map=variant_image_map,
            variant_labels=variant_labels,
            inventory_count=inventory_count,
            inventory_value=inventory_value,
            missing_fields=missing_fields,
            confidence=merch.confidence,
            source_url=source_url,
        ))

    run_id = run_dir.name
    run = ApplyRun(run_id=run_id, source_dir=str(run_dir), changes=changes)

    output_path = out or (run_dir / "review.html")
    generate_review_report(run, output_path)
    console.print(f"Review report: [green]{output_path}[/green] ({len(changes)} products)")

    if serve:
        from lookout.review.server import serve_review
        dispositions_path = run_dir / f"{run_id}_dispositions.json"
        serve_review(output_path, dispositions_path, port=port)
    else:
        console.print("Open in your browser, review each product, then Save Dispositions.")


@enrich.command("apply")
@click.option("--run-dir", "-d", required=True, type=click.Path(exists=True, path_type=Path),
              help="Enrichment output directory")
@click.option("--dispositions", "-r", required=True, type=click.Path(exists=True, path_type=Path),
              help="Dispositions JSON from review")
@click.option("--backup-dir", type=click.Path(path_type=Path), default=Path("./backups"),
              help="Directory for pre-write backups")
@click.option("--dry-run", is_flag=True, help="Show what would be applied without writing")
@click.option("--push/--no-push", default=False, help="Actually write to Shopify (default: off)")
@click.option("--verbose", is_flag=True)
def apply_cmd(run_dir, dispositions, backup_dir, dry_run, push, verbose):
    """Process review dispositions and collect feedback.

    By default, collects feedback only (no Shopify writes).
    Use --push to actually write approved changes to Shopify.
    """
    setup_logging(verbose)
    import json as json_mod
    from lookout.apply.models import ApplyRun, ProductChange, ChangeStatus
    from lookout.apply.writer import apply_run
    from lookout.enrich.models import MerchOutput
    from lookout.feedback.collector import collect_feedback, save_feedback
    from lookout.review.dispositions import load_dispositions, apply_dispositions_to_run
    from lookout.store import LookoutStore

    store = LookoutStore()

    # Build changes (same as review command)
    changes = []
    for handle_dir in sorted(Path(run_dir).iterdir()):
        if not handle_dir.is_dir():
            continue
        merch_path = handle_dir / "merch_output.json"
        if not merch_path.exists():
            continue
        merch = MerchOutput(**json_mod.loads(merch_path.read_text()))
        product = store.get_product(merch.handle)
        if not product:
            continue
        changes.append(ProductChange(
            handle=merch.handle, product_id=product["id"],
            title=product.get("title", ""), vendor=product.get("vendor", ""),
            current_body_html=product.get("body_html", ""),
            new_body_html=merch.body_html, confidence=merch.confidence,
        ))

    run = ApplyRun(run_id=Path(run_dir).name, source_dir=str(run_dir), changes=changes)

    # Apply dispositions
    disps = load_dispositions(Path(dispositions))
    apply_dispositions_to_run(run, disps)

    approved = run.approved
    rejected = run.rejected

    console.print(f"Approved: [green]{len(approved)}[/green]  Rejected: [red]{len(rejected)}[/red]  Pending: {len(run.pending)}")

    if dry_run:
        console.print("\n[yellow]DRY RUN — no changes will be made[/yellow]")
        for c in approved:
            label = "EDITED" if c.status == ChangeStatus.EDITED else "APPROVED"
            console.print(f"  [{label}] {c.handle} ({c.vendor})")
        return

    if push and not dry_run and approved:
        # Apply to Shopify
        from tvr.mcp.api import ShopifyAdminAPI
        from tvr.mcp.auth import ShopifyAuth
        auth = ShopifyAuth()
        api = ShopifyAdminAPI(auth)
        asyncio.run(apply_run(run, api, Path(backup_dir)))

        applied = run.applied
        failed = [c for c in run.changes if c.status == ChangeStatus.FAILED]
        console.print(f"\nApplied: [green]{len(applied)}[/green]  Failed: [red]{len(failed)}[/red]")

        for c in failed:
            console.print(f"  [red]FAILED[/red] {c.handle}: {c.error}")
    elif approved and not dry_run:
        console.print(f"\n[dim]{len(approved)} approved changes ready. Use --push to write to Shopify.[/dim]")

    # Collect and save feedback (from ALL dispositions, not just applied)
    feedback_entries = collect_feedback(run)
    if feedback_entries:
        feedback_dir = Path(run_dir) / "feedback"
        save_feedback(feedback_entries, feedback_dir)
        console.print(f"\nFeedback saved: {len(feedback_entries)} entries to {feedback_dir}")


@enrich.command("revert")
@click.option("--handle", "-h", "handles", multiple=True, help="Product handles to revert (repeatable)")
@click.option("--run-dir", "-d", type=click.Path(exists=True, path_type=Path), help="Revert all products from a run")
@click.option("--backup-dir", type=click.Path(exists=True, path_type=Path), default=Path("./backups"),
              help="Directory containing backups")
@click.option("--verbose", is_flag=True)
def revert_cmd(handles, run_dir, backup_dir, verbose):
    """Revert applied enrichment changes from backup.

    Restores the previous Shopify state for specified products.
    """
    setup_logging(verbose)
    from lookout.apply.revert import revert_change

    handles = list(handles)

    if not handles and not run_dir:
        console.print("[red]Provide --handle or --run-dir[/red]")
        sys.exit(1)

    if run_dir:
        # Find all applied products from run feedback
        import json as json_mod
        feedback_dir = Path(run_dir) / "feedback"
        if feedback_dir.exists():
            for f in feedback_dir.glob("*_approved.json"):
                data = json_mod.loads(f.read_text())
                handles.append(data["handle"])

    from tvr.mcp.api import ShopifyAdminAPI
    from tvr.mcp.auth import ShopifyAuth
    auth = ShopifyAuth()
    api = ShopifyAdminAPI(auth)

    reverted = 0
    for handle in handles:
        success = asyncio.run(revert_change(handle, Path(backup_dir), api))
        if success:
            console.print(f"  [green]Reverted[/green] {handle}")
            reverted += 1
        else:
            console.print(f"  [red]No backup[/red] {handle}")

    console.print(f"\nReverted {reverted}/{len(handles)} products")


@enrich.command("feedback")
@click.option("--feedback-dir", "-d", type=click.Path(exists=True, path_type=Path),
              default=None, help="Feedback directory to summarize")
@click.option("--all-runs", is_flag=True, help="Aggregate feedback across all campaign runs")
@click.option("--verbose", is_flag=True)
def feedback_cmd(feedback_dir, all_runs, verbose):
    """Show feedback summary from review dispositions.

    Displays approval rate, rejection reasons, and trends over time.
    """
    setup_logging(verbose)
    from lookout.feedback.collector import load_all_feedback, feedback_summary

    if all_runs:
        all_entries = []
        for run_dir in sorted(Path("campaign").glob("run_*/feedback")):
            all_entries.extend(load_all_feedback(run_dir))
        entries = all_entries
    elif feedback_dir:
        entries = load_all_feedback(Path(feedback_dir))
    else:
        console.print("[red]Provide --feedback-dir or --all-runs[/red]")
        sys.exit(1)

    if not entries:
        console.print("[yellow]No feedback entries found.[/yellow]")
        return

    summary = feedback_summary(entries)

    table = Table(title="Feedback Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total reviewed", str(summary["total"]))
    table.add_row("Approved", f"[green]{summary['approved']}[/green]")
    table.add_row("Rejected", f"[red]{summary['rejected']}[/red]")
    table.add_row("Edited", f"[yellow]{summary['edited']}[/yellow]")
    table.add_row("Approval rate", f"{summary['approval_rate']:.0%}")
    console.print(table)

    if summary["rejection_reasons"]:
        rtable = Table(title="Rejection Reasons")
        rtable.add_column("Reason", style="cyan")
        rtable.add_column("Count", style="red")
        for reason, count in sorted(summary["rejection_reasons"].items(), key=lambda x: -x[1]):
            rtable.add_row(reason, str(count))
        console.print(rtable)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
