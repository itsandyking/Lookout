"""Unified CLI for Lookout — merchandising command center."""

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()
from rich.console import Console  # noqa: E402
from rich.logging import RichHandler  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

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
    "--out",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Export priority CSV",
)
@click.option("--include-house-brands", is_flag=True, help="Include house brands in audit")
@click.option(
    "--online/--no-online",
    default=False,
    help="Enrich with online opportunity signals (sessions, conversion)",
)
@click.option(
    "--gmc/--no-gmc",
    default=False,
    help="Enrich with Google Merchant Center signals (clicks, impressions, disapprovals)",
)
@click.option(
    "--lookback", default=90, help="Days to look back for online/GMC signals (default 90)"
)
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
                for p in all_products
                if p.get("title") and p.get("handle")
            }

            client = ShopifyQLClient.from_config()
            online_signals = asyncio.run(
                fetch_online_signals(
                    client,
                    title_to_handle=title_to_handle,
                    lookback_days=lookback,
                )
            )
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
@click.option(
    "--out", "-o", required=True, type=click.Path(path_type=Path), help="Output snapshot JSON path"
)
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
                for p in all_products
                if p.get("title") and p.get("handle")
            }

            client = ShopifyQLClient.from_config()
            online_signals = asyncio.run(
                fetch_online_signals(
                    client,
                    title_to_handle=title_to_handle,
                    lookback_days=lookback,
                )
            )
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

    console.print(
        f"Audited {summary['total_products']} products, {summary['products_with_gaps']} with gaps"
    )

    save_snapshot(result.scores, Path(out))
    console.print(f"Snapshot saved to [green]{out}[/green]")


@cli.command("audit-optimize")
@click.option(
    "--snapshot",
    "-s",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Snapshot JSON from audit-snapshot",
)
@click.option("--top-n", default=50, help="Top-N products to optimize for (default 50)")
@click.option(
    "--log-dir",
    "-l",
    default=Path("./audit_optimize_log"),
    type=click.Path(path_type=Path),
    help="Directory for optimization logs",
)
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

    console.print(
        Panel(
            f"[bold]Audit Weight Optimization[/bold]\n"
            f"Snapshot: {snapshot} ({len(scores)} products)\n"
            f"Optimizing top-{top_n} coverage efficiency\n"
            f"Max iterations: {max_iterations}",
            title="Optimize",
        )
    )

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

    btable.add_row(
        "Variant leverage (0.30)", f"{bl['variant_leverage']:.1%}", f"{bt['variant_leverage']:.1%}"
    )
    btable.add_row(
        "Vendor concentration (0.20)",
        f"{bl['vendor_concentration']:.1%}",
        f"{bt['vendor_concentration']:.1%}",
    )
    btable.add_row(
        "Inventory coverage (0.25)",
        f"{bl['inventory_coverage']:.1%}",
        f"{bt['inventory_coverage']:.1%}",
    )
    btable.add_row(
        "Traffic alignment (0.25)",
        f"{bl['traffic_alignment']:.1%}",
        f"{bt['traffic_alignment']:.1%}",
    )
    btable.add_row(
        "[bold]Composite[/bold]",
        f"[bold]{bl['composite']:.1%}[/bold]",
        f"[bold green]{bt['composite']:.1%}[/bold green]",
    )

    console.print(btable)

    if bt.get("top_vendors"):
        console.print("\n[bold]Top vendors in optimized batch:[/bold]")
        for vendor, count in bt["top_vendors"]:
            console.print(f"  {vendor}: {count} products")

    console.print(
        f"\n[bold]Improvement:[/bold] {result['best_efficiency'] - result['baseline_efficiency']:+.4f}"
    )
    console.print(f"[bold]Logs:[/bold] {log_dir}")


@cli.command("shipping")
@click.option(
    "--out",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
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
    from lookout.audit.shipping import export_shipping_audit_csv, run_shipping_audit
    from lookout.store import LookoutStore

    try:
        store = LookoutStore()
    except Exception as e:
        console.print(f"[red]Error connecting to database:[/red] {e}")
        sys.exit(1)

    issues = run_shipping_audit(store)

    # Summary
    [i for i in issues if i.severity == "critical"]
    warnings = [i for i in issues if i.severity == "warning"]
    info = [i for i in issues if i.severity == "info"]

    zero_weight = [i for i in issues if i.issue_type == "zero_weight"]
    [i for i in issues if i.issue_type == "weight_mismatch"]
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
        console.print(
            f"\n[yellow bold]High shipping-to-price ratio ({len(high_ratio)}):[/yellow bold]"
        )
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
    "-i",
    "--input",
    "input_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Input CSV (optional — audits internally if not provided)",
)
@click.option("--vendor", "-v", default=None, help="Filter by vendor (used with internal audit)")
@click.option(
    "--handle",
    "-h",
    "handles",
    multiple=True,
    help="Specific product handles to enrich (repeatable)",
)
@click.option(
    "--out",
    "-o",
    "output_dir",
    type=click.Path(path_type=Path),
    default=Path("./output"),
    help="Output directory",
)
@click.option(
    "--vendors",
    "vendors_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to vendors.yaml",
)
@click.option("--concurrency", "-c", type=int, default=5, help="Max concurrent requests")
@click.option("--max-rows", "-n", type=int, default=None, help="Max rows to process")
@click.option("--force", "-f", is_flag=True, help="Force re-processing")
@click.option("--dry-run", is_flag=True, help="Process but don't write output")
@click.option("--verify", is_flag=True, help="Enable LLM fact-checking of generated descriptions")
@click.option(
    "--only",
    "only_mode",
    type=click.Choice(["images", "description", "variant-images"]),
    default=None,
    help="Only fill this specific gap type",
)
@click.option(
    "--llm",
    "llm_provider",
    type=click.Choice(["claude", "reason", "ollama", "hybrid", "sdk"]),
    default=None,
    help="LLM provider: hybrid=reason+claude split (default: auto-detect)",
)
@click.option(
    "--brave-images/--no-brave-images",
    "brave_images",
    default=None,
    help="Enable/disable Brave Image Search fallback (default: from vendors.yaml)",
)
@click.option("--verbose", is_flag=True)
def run(
    input_path,
    vendor,
    handles,
    output_dir,
    vendors_path,
    concurrency,
    max_rows,
    force,
    dry_run,
    verify,
    only_mode,
    llm_provider,
    brave_images,
    verbose,
):
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
        llm_provider=llm_provider,
        brave_images=brave_images,
    )

    console.print(
        Panel(
            f"[bold]Input:[/bold] {input_path}\n"
            f"[bold]Output:[/bold] {output_dir}\n"
            f"[bold]Vendors:[/bold] {vendors_path}\n"
            f"[bold]Concurrency:[/bold] {concurrency}",
            title="Enrichment Pipeline",
        )
    )

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
    "--output-dir",
    "-d",
    "output_dir",
    type=click.Path(exists=True, path_type=Path),
    default=Path("./output"),
    help="Enrichment output directory to score",
)
@click.option(
    "--handle", "-h", "handles", multiple=True, help="Specific handles to score (repeatable)"
)
@click.option(
    "--verify/--no-verify", default=False, help="Run LLM fact-checking (requires API key)"
)
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
        from lookout.enrich.llm import get_llm_client
        from lookout.enrich.scorer import load_artifacts

        llm = get_llm_client()

        # Determine handles to verify
        if handle_list is None:
            handle_list = [
                d.name
                for d in sorted(output_dir.iterdir())
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
        axis_names = [
            "factual_fidelity",
            "structural_compliance",
            "length_targets",
            "anti_hype",
            "coverage",
        ]
        for name in axis_names:
            vals = [s.axes[name].score for s in scores if name in s.axes]
            maxes = [s.axes[name].max_score for s in scores if name in s.axes]
            if vals:
                avg = sum(vals) / len(vals)
                max_val = maxes[0]
                console.print(f"  {name}: {avg:.1f}/{max_val}")


@enrich.command("score-facts")
@click.option(
    "--test-dir",
    "-t",
    type=click.Path(exists=True, path_type=Path),
    default=Path("./test_set/combined"),
    help="Directory with extracted facts",
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
    "--test-dir",
    "-t",
    "test_dir",
    type=click.Path(exists=True, path_type=Path),
    default=Path("./test_set/combined"),
    help="Test set directory with cached artifacts",
)
@click.option(
    "--prompt",
    "-p",
    "prompt_path",
    type=click.Path(exists=True, path_type=Path),
    default=Path("./lookout/enrich/prompts/generate_body_html.prompt"),
    help="Prompt file to optimize",
)
@click.option(
    "--log-dir",
    "-l",
    "log_dir",
    type=click.Path(path_type=Path),
    default=Path("./test_set/optimize_log"),
    help="Directory for iteration logs",
)
@click.option("--max-iterations", "-n", default=5, help="Maximum optimization iterations")
@click.option(
    "--feedback-dir",
    "-f",
    "feedback_dir",
    type=click.Path(exists=True, path_type=Path),
    default=None,
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
    console.print(
        Panel(
            f"[bold]Karpathy Loop: Prompt Optimization[/bold]\n"
            f"Test set: {test_dir}\n"
            f"Prompt: {prompt_path}\n"
            f"Max iterations: {max_iterations}{feedback_info}",
            title="Optimize",
        )
    )

    try:
        history = asyncio.run(
            run_optimization_loop(
                test_dir=test_dir,
                prompt_path=prompt_path,
                log_dir=log_dir,
                max_iterations=max_iterations,
                feedback_dir=feedback_dir,
            )
        )
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
    "--out",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
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
        collection=collection,
        vendor=vendor,
        product_type=product_type,
        limit=limit,
    )

    if output_path:
        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Rank",
                    "Handle",
                    "Title",
                    "Vendor",
                    "Score",
                    "Weekly Units",
                    "Margin %",
                    "Inventory",
                    "WOS",
                ]
            )
            for p in result.ranked:
                writer.writerow(
                    [
                        p.rank,
                        p.handle,
                        p.title,
                        p.vendor,
                        f"{p.total_score:.3f}",
                        f"{p.weekly_units:.1f}",
                        f"{p.margin_pct:.1f}",
                        p.total_inventory,
                        f"{p.weeks_of_supply:.1f}",
                    ]
                )
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
                str(p.rank),
                p.title[:40],
                p.vendor,
                f"{p.total_score:.2f}",
                f"{p.weekly_units:.1f}",
                str(p.total_inventory),
            )
        console.print(table)


# ---------------------------------------------------------------------------
# vendors
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--vendors",
    "vendors_path",
    type=click.Path(path_type=Path),
    default=None,
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
    "--out",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
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
    "--out",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
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


@output.command("push-gmc-attributes")
@click.option("--vendor", "-v", multiple=True, help="Limit to specific vendors (repeatable)")
@click.option(
    "--handle", "-h", multiple=True, help="Limit to specific product handles (repeatable)"
)
@click.option("--dry-run", is_flag=True, help="Preview what would be pushed, no writes")
@click.option("--verbose", is_flag=True)
def push_gmc_attributes(vendor, handle, dry_run, verbose):
    """Push google_shopping.gender + age_group metafields directly to Shopify API.

    Derives values from product tags and title using the same logic as the
    google-shopping XLSX command. Skips excluded vendors and blank gender values.
    Batches ~12 products (up to 25 metafields) per API call.
    """
    import asyncio as _asyncio
    import time

    setup_logging(verbose)

    from lookout.output.google_shopping import get_age_group, get_gender
    from lookout.store import LookoutStore
    from lookout.taxonomy.mappings import EXCLUDED_VENDORS

    store = LookoutStore()
    vendor_filter = {v.lower() for v in vendor}
    handle_filter = {h.lower() for h in handle}
    products = store.list_products()

    METAFIELDS_SET = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
        metafieldsSet(metafields: $metafields) {
            metafields { key namespace ownerType }
            userErrors { field message }
        }
    }
    """

    # Build (product_summary, metafields_list) pairs for all qualifying products
    entries = []
    skipped = 0
    for p in products:
        p_vendor = (p["vendor"] or "").strip()
        if p_vendor.lower() in EXCLUDED_VENDORS:
            skipped += 1
            continue
        if vendor_filter and p_vendor.lower() not in vendor_filter:
            continue
        if handle_filter and (p["handle"] or "").lower() not in handle_filter:
            continue

        tags = p["tags"] or ""
        title = p["title"] or ""
        gender = get_gender(tags, title)
        age_group = get_age_group(tags, title)
        owner_id = f"gid://shopify/Product/{p['id']}"

        mfs = []
        for ns in ("google_shopping", "mm-google-shopping"):
            if gender:
                mfs.append(
                    {
                        "ownerId": owner_id,
                        "namespace": ns,
                        "key": "gender",
                        "value": gender,
                        "type": "single_line_text_field",
                    }
                )
            mfs.append(
                {
                    "ownerId": owner_id,
                    "namespace": ns,
                    "key": "age_group",
                    "value": age_group,
                    "type": "single_line_text_field",
                }
            )
        entries.append((p["handle"], gender, age_group, mfs))

    console.print(
        f"[dim]Products to push: {len(entries)}  |  Skipped (excluded vendor): {skipped}[/dim]"
    )

    if dry_run:
        console.print("[yellow]DRY RUN — no changes written[/yellow]")
        for handle, gender, age_group, _ in entries[:10]:
            console.print(f"  {handle} | gender={gender or '(skip)'} | age_group={age_group}")
        if len(entries) > 10:
            console.print(f"  ... and {len(entries) - 10} more")
        return

    # Batch into groups of up to 25 metafields
    batches: list[list[dict]] = []
    current: list[dict] = []
    for _, _, _, mfs in entries:
        if len(current) + len(mfs) > 25:
            batches.append(current)
            current = []
        current.extend(mfs)
    if current:
        batches.append(current)

    total_mfs = sum(len(b) for b in batches)
    console.print(f"[dim]Batches: {len(batches)}  |  Total metafields: {total_mfs}[/dim]")

    import json as _json

    with open(_SHOPIFY_CONFIG_PATH) as _f:
        _cfg = _json.load(_f)

    _store_url = _cfg["store_url"]
    _access_token = _cfg["access_token"]
    _api_version = _cfg.get("api_version", "2026-04")
    _graphql_url = f"https://{_store_url}/admin/api/{_api_version}/graphql.json"
    _headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": _access_token}

    async def push_all():
        import httpx

        pushed = failed = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, batch in enumerate(batches, 1):
                if verbose:
                    console.print(f"[dim]Batch {i}/{len(batches)} ({len(batch)} metafields)[/dim]")
                while True:
                    resp = await client.post(
                        _graphql_url,
                        json={"query": METAFIELDS_SET, "variables": {"metafields": batch}},
                        headers=_headers,
                    )
                    if resp.status_code == 429:
                        wait = float(resp.headers.get("Retry-After", "2.0"))
                        console.print(f"[yellow]Rate limited, sleeping {wait:.1f}s[/yellow]")
                        await _asyncio.sleep(wait)
                        continue
                    if resp.status_code >= 500:
                        console.print(
                            f"[yellow]Server error {resp.status_code}, retrying in 3s[/yellow]"
                        )
                        await _asyncio.sleep(3.0)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    errors = data.get("data", {}).get("metafieldsSet", {}).get("userErrors", [])
                    if errors:
                        for e in errors:
                            console.print(f"[red]metafieldsSet error: {e}[/red]")
                        failed += 1
                    else:
                        pushed += 1
                    await _asyncio.sleep(0.5)
                    break

        return pushed, failed

    t0 = time.time()
    pushed, failed = _asyncio.run(push_all())
    elapsed = time.time() - t0

    console.print(
        f"Done in {elapsed:.0f}s — [green]{pushed}[/green] batches OK, [red]{failed}[/red] failed"
    )


@output.command("push-gmc-category")
@click.option("--vendor", "-v", multiple=True, help="Limit to specific vendors (repeatable)")
@click.option(
    "--handle", "-h", multiple=True, help="Limit to specific product handles (repeatable)"
)
@click.option("--dry-run", is_flag=True, help="Preview what would be pushed, no writes")
@click.option("--verbose", is_flag=True)
def push_gmc_category(vendor, handle, dry_run, verbose):
    """Push google_shopping.google_product_category metafields directly to Shopify API.

    Derives the Google category from TMA Product Type via PRODUCT_TYPE_TO_GOOGLE_CATEGORY.
    Skips products with no mappable product type (Used, Sample, blank, internal types).
    Batches up to 25 metafields per API call.
    """
    import asyncio as _asyncio
    import json as _json
    import time

    setup_logging(verbose)

    from lookout.output.google_shopping import get_google_category
    from lookout.store import LookoutStore
    from lookout.taxonomy.mappings import EXCLUDED_VENDORS

    SKIP_TYPES = {
        "used",
        "sample",
        "samples",
        "rental",
        "new",
        "",
        "shop work non-taxable",
        "shop work taxable",
        "credit",
        "gift cards",
        "gift",
        "test",
    }

    store = LookoutStore()
    vendor_filter = {v.lower() for v in vendor}
    handle_filter = {h.lower() for h in handle}
    products = store.list_products()

    METAFIELDS_SET = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
        metafieldsSet(metafields: $metafields) {
            metafields { key namespace ownerType }
            userErrors { field message }
        }
    }
    """

    entries = []
    skipped = 0
    for p in products:
        p_vendor = (p["vendor"] or "").strip()
        pt = (p["product_type"] or "").strip()

        if p_vendor.lower() in EXCLUDED_VENDORS or pt.lower() in SKIP_TYPES:
            skipped += 1
            continue
        if vendor_filter and p_vendor.lower() not in vendor_filter:
            continue
        if handle_filter and (p["handle"] or "").lower() not in handle_filter:
            continue

        category = get_google_category(pt)
        if not category:
            continue

        owner_id = f"gid://shopify/Product/{p['id']}"
        entries.append((p["handle"], pt, category, owner_id))

    console.print(
        f"[dim]Products to push: {len(entries)}  |  Skipped (no category/excluded): {skipped}[/dim]"
    )

    if dry_run:
        console.print("[yellow]DRY RUN — no changes written[/yellow]")
        for handle, pt, cat, _ in entries[:10]:
            console.print(f"  {handle} | {pt}  →  {cat}")
        if len(entries) > 10:
            console.print(f"  ... and {len(entries) - 10} more")
        return

    # Batch up to 25 metafields
    batches: list[list[dict]] = []
    current: list[dict] = []
    for _, _, category, owner_id in entries:
        for ns in ("google_shopping", "mm-google-shopping"):
            current.append(
                {
                    "ownerId": owner_id,
                    "namespace": ns,
                    "key": "google_product_category",
                    "value": category,
                    "type": "single_line_text_field",
                }
            )
        if len(current) >= 24:
            batches.append(current)
            current = []
    if current:
        batches.append(current)

    console.print(
        f"[dim]Batches: {len(batches)}  |  Total metafields: {sum(len(b) for b in batches)}[/dim]"
    )

    with open(_SHOPIFY_CONFIG_PATH) as _f:
        _cfg = _json.load(_f)
    _store_url = _cfg["store_url"]
    _access_token = _cfg["access_token"]
    _api_version = _cfg.get("api_version", "2026-04")
    _graphql_url = f"https://{_store_url}/admin/api/{_api_version}/graphql.json"
    _headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": _access_token}

    async def push_all():
        import httpx

        pushed = failed = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, batch in enumerate(batches, 1):
                if verbose:
                    console.print(f"[dim]Batch {i}/{len(batches)}[/dim]")
                while True:
                    resp = await client.post(
                        _graphql_url,
                        json={"query": METAFIELDS_SET, "variables": {"metafields": batch}},
                        headers=_headers,
                    )
                    if resp.status_code == 429:
                        wait = float(resp.headers.get("Retry-After", "2.0"))
                        console.print(f"[yellow]Rate limited, sleeping {wait:.1f}s[/yellow]")
                        await _asyncio.sleep(wait)
                        continue
                    if resp.status_code >= 500:
                        console.print(
                            f"[yellow]Server error {resp.status_code}, retrying in 3s[/yellow]"
                        )
                        await _asyncio.sleep(3.0)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    errors = data.get("data", {}).get("metafieldsSet", {}).get("userErrors", [])
                    if errors:
                        for e in errors:
                            console.print(f"[red]metafieldsSet error: {e}[/red]")
                        failed += 1
                    else:
                        pushed += 1
                    await _asyncio.sleep(0.5)
                    break
        return pushed, failed

    t0 = time.time()
    pushed, failed = _asyncio.run(push_all())
    elapsed = time.time() - t0
    console.print(
        f"Done in {elapsed:.0f}s — [green]{pushed}[/green] batches OK, [red]{failed}[/red] failed"
    )


@output.command("exclude-from-shopping")
@click.option("--dry-run", is_flag=True, help="Preview what would be excluded, no writes")
@click.option("--verbose", is_flag=True)
def exclude_from_shopping(dry_run, verbose):
    """Unpublish service/rental products from the Google & YouTube channel.

    Targets:
      - vendor IN ('The Mountain Air Back Shop', 'The Mountain Air Deposits')
      - title LIKE '%rental%' (excluding Switchback used-gear and sale items)
    """
    import asyncio as _asyncio
    import json as _json

    setup_logging(verbose)

    GOOGLE_PUBLICATION_ID = "gid://shopify/Publication/55692951597"

    PUBLISHABLE_UNPUBLISH = """
    mutation publishableUnpublish($id: ID!, $input: PublicationInput!) {
      publishableUnpublish(id: $id, input: $input) {
        publishable { ... on Product { id title } }
        userErrors { field message }
      }
    }
    """

    # Query Dolt directly — LookoutStore doesn't expose raw SQL for this filter
    import pymysql

    conn = pymysql.connect(
        host="100.122.28.91",
        port=3306,
        user="tvr",
        password="",
        database="shopify",
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, vendor, product_type
                FROM products
                WHERE status = 'active'
                AND (
                    vendor IN ('The Mountain Air Back Shop', 'The Mountain Air Deposits')
                    OR (
                        LOWER(title) LIKE '%rental%'
                        AND vendor NOT LIKE '%Switchback%'
                        AND LOWER(title) NOT LIKE '%sale%'
                    )
                )
                ORDER BY title
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    console.print(f"[bold]Products to exclude from Google & YouTube:[/bold] {len(rows)}")
    for product_id, title, vendor, product_type in rows:
        console.print(f"  [dim]{product_id}[/dim]  {title}  [dim]({vendor})[/dim]")

    if dry_run:
        console.print(
            f"\n[yellow]DRY RUN — {len(rows)} products would be excluded. No changes written.[/yellow]"
        )
        return

    with open(_SHOPIFY_CONFIG_PATH) as _f:
        _cfg = _json.load(_f)
    _store_url = _cfg["store_url"]
    _access_token = _cfg["access_token"]
    _api_version = _cfg.get("api_version", "2026-04")
    _graphql_url = f"https://{_store_url}/admin/api/{_api_version}/graphql.json"
    _headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": _access_token}

    async def unpublish_all():
        import httpx

        ok = failed = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            for product_id, title, vendor, product_type in rows:
                gid = f"gid://shopify/Product/{product_id}"
                while True:
                    resp = await client.post(
                        _graphql_url,
                        json={
                            "query": PUBLISHABLE_UNPUBLISH,
                            "variables": {
                                "id": gid,
                                "input": {"publicationId": GOOGLE_PUBLICATION_ID},
                            },
                        },
                        headers=_headers,
                    )
                    if resp.status_code == 429:
                        wait = float(resp.headers.get("Retry-After", "2.0"))
                        console.print(f"[yellow]Rate limited, sleeping {wait:.1f}s[/yellow]")
                        await _asyncio.sleep(wait)
                        continue
                    if resp.status_code >= 500:
                        console.print(
                            f"[yellow]Server error {resp.status_code}, retrying in 3s[/yellow]"
                        )
                        await _asyncio.sleep(3.0)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    errors = (
                        data.get("data", {}).get("publishableUnpublish", {}).get("userErrors", [])
                    )
                    if errors:
                        for e in errors:
                            console.print(f"[red]  {title}: {e['message']}[/red]")
                        failed += 1
                    else:
                        console.print(f"[green]  Excluded:[/green] {title}")
                        ok += 1
                    await _asyncio.sleep(0.3)
                    break
        return ok, failed

    ok, failed = _asyncio.run(unpublish_all())
    console.print(f"\n[bold]Done[/bold] — [green]{ok}[/green] excluded, [red]{failed}[/red] failed")


@output.command("push-gmc-color")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(["goodr", "tma"], case_sensitive=False),
    required=True,
    help="Vendor group: 'goodr' or 'tma'",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be pushed, no writes")
@click.option("--verbose", is_flag=True)
def push_gmc_color(vendor, dry_run, verbose):
    """Push google_shopping.color metafields for Goodr and TMA branded items.

    Goodr: color = full product title (the colorway IS the title).
    TMA: color = leading color word(s) extracted from title.
    Pushes to both google_shopping and mm-google-shopping namespaces.
    """
    import asyncio as _asyncio
    import json as _json
    import re
    import time

    setup_logging(verbose)

    from lookout.store import LookoutStore

    # Color words to detect at start of TMA product titles
    TMA_COLOR_WORDS = [
        "Black",
        "White",
        "Navy",
        "Gray",
        "Grey",
        "Red",
        "Blue",
        "Green",
        "Yellow",
        "Orange",
        "Purple",
        "Pink",
        "Brown",
        "Tan",
        "Olive",
        "Slate",
        "Charcoal",
        "Natural",
        "Stone",
    ]
    TMA_COLOR_PATTERN = re.compile(
        r"^(" + "|".join(re.escape(c) for c in TMA_COLOR_WORDS) + r")(?:\s+|$)",
        re.IGNORECASE,
    )

    TMA_SUFFIX = " | The Mountain Air"

    def extract_color(p: dict, vendor_group: str) -> str | None:
        title = (p["title"] or "").strip()
        if vendor_group == "goodr":
            # Strip trailing store suffix if present
            if title.lower().endswith(TMA_SUFFIX.lower()):
                title = title[: -len(TMA_SUFFIX)].strip()
            return title or None
        else:  # tma
            m = TMA_COLOR_PATTERN.match(title)
            return m.group(1).capitalize() if m else None

    store = LookoutStore()
    products = store.list_products()

    # Filter products by vendor group
    if vendor == "goodr":
        vendor_match = lambda v: v.lower() == "goodr"  # noqa: E731
    else:
        vendor_match = lambda v: "mountain air" in v.lower() or v.lower() == "tma"  # noqa: E731

    METAFIELDS_SET = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
        metafieldsSet(metafields: $metafields) {
            metafields { key namespace ownerType }
            userErrors { field message }
        }
    }
    """

    entries = []
    skipped_no_color = 0
    for p in products:
        p_vendor = (p["vendor"] or "").strip()
        if not vendor_match(p_vendor):
            continue
        color = extract_color(p, vendor)
        if not color:
            skipped_no_color += 1
            continue
        owner_id = f"gid://shopify/Product/{p['id']}"
        entries.append((p["handle"], p["title"], color, owner_id))

    console.print(
        f"[dim]Vendor group: {vendor}  |  Products to push: {len(entries)}"
        f"  |  Skipped (no color): {skipped_no_color}[/dim]"
    )

    if dry_run:
        console.print("[yellow]DRY RUN — no changes written[/yellow]")
        for handle, title, color, _ in entries[:20]:
            console.print(f"  {handle} | title={title!r}  →  color={color!r}")
        if len(entries) > 20:
            console.print(f"  ... and {len(entries) - 20} more")
        return

    # Batch up to 25 metafields (2 per product: google_shopping + mm-google-shopping)
    batches: list[list[dict]] = []
    current: list[dict] = []
    for _, _, color, owner_id in entries:
        for ns in ("google_shopping", "mm-google-shopping"):
            current.append(
                {
                    "ownerId": owner_id,
                    "namespace": ns,
                    "key": "color",
                    "value": color,
                    "type": "single_line_text_field",
                }
            )
        if len(current) >= 24:
            batches.append(current)
            current = []
    if current:
        batches.append(current)

    console.print(
        f"[dim]Batches: {len(batches)}  |  Total metafields: {sum(len(b) for b in batches)}[/dim]"
    )

    with open(_SHOPIFY_CONFIG_PATH) as _f:
        _cfg = _json.load(_f)
    _store_url = _cfg["store_url"]
    _access_token = _cfg["access_token"]
    _api_version = _cfg.get("api_version", "2026-04")
    _graphql_url = f"https://{_store_url}/admin/api/{_api_version}/graphql.json"
    _headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": _access_token}

    async def push_all():
        import httpx

        pushed = failed = 0
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, batch in enumerate(batches, 1):
                if verbose:
                    console.print(f"[dim]Batch {i}/{len(batches)} ({len(batch)} metafields)[/dim]")
                while True:
                    resp = await client.post(
                        _graphql_url,
                        json={"query": METAFIELDS_SET, "variables": {"metafields": batch}},
                        headers=_headers,
                    )
                    if resp.status_code == 429:
                        wait = float(resp.headers.get("Retry-After", "2.0"))
                        console.print(f"[yellow]Rate limited, sleeping {wait:.1f}s[/yellow]")
                        await _asyncio.sleep(wait)
                        continue
                    if resp.status_code >= 500:
                        console.print(
                            f"[yellow]Server error {resp.status_code}, retrying in 3s[/yellow]"
                        )
                        await _asyncio.sleep(3.0)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    errors = data.get("data", {}).get("metafieldsSet", {}).get("userErrors", [])
                    if errors:
                        for e in errors:
                            console.print(f"[red]metafieldsSet error: {e}[/red]")
                        failed += 1
                    else:
                        pushed += 1
                    await _asyncio.sleep(0.5)
                    break
        return pushed, failed

    t0 = time.time()
    pushed, failed = _asyncio.run(push_all())
    elapsed = time.time() - t0
    console.print(
        f"Done in {elapsed:.0f}s — [green]{pushed}[/green] batches OK, [red]{failed}[/red] failed"
    )


@output.command("weights")
@click.option(
    "--out",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
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
    "--out",
    "-o",
    "output_path",
    type=click.Path(path_type=Path),
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
@click.option(
    "--run-dir",
    "-d",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Enrichment output directory to review",
)
@click.option(
    "--out",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output HTML report path (default: {run-dir}/review.html)",
)
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

        # Get source URL from merch_output or resolver output
        source_url = merch.source_url
        if not source_url:
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
        new_images = [
            img.model_dump() if hasattr(img, "model_dump") else img for img in (merch.images or [])
        ]
        current_images = product.get("images", [])
        variant_image_map = dict(merch.variant_image_map) if merch.variant_image_map else None

        # Get variant labels for display
        variants = store.get_variants(product["id"])
        variant_labels = []
        for v in variants:
            parts = []
            for i in (1, 2, 3):
                v.get(f"option{i}_name", "")
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

        changes.append(
            ProductChange(
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
            )
        )

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
@click.option(
    "--run-dir",
    "-d",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Enrichment output directory",
)
@click.option(
    "--dispositions",
    "-r",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Dispositions JSON from review",
)
@click.option(
    "--backup-dir",
    type=click.Path(path_type=Path),
    default=Path("./backups"),
    help="Directory for pre-write backups",
)
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

    from lookout.apply.models import ApplyRun, ChangeStatus, ProductChange
    from lookout.apply.writer import apply_run
    from lookout.enrich.models import MerchOutput
    from lookout.feedback.collector import collect_feedback, save_feedback
    from lookout.review.dispositions import apply_dispositions_to_run, load_dispositions
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
        changes.append(
            ProductChange(
                handle=merch.handle,
                product_id=product["id"],
                title=product.get("title", ""),
                vendor=product.get("vendor", ""),
                current_body_html=product.get("body_html", ""),
                new_body_html=merch.body_html,
                confidence=merch.confidence,
            )
        )

    run = ApplyRun(run_id=Path(run_dir).name, source_dir=str(run_dir), changes=changes)

    # Apply dispositions
    disps = load_dispositions(Path(dispositions))
    apply_dispositions_to_run(run, disps)

    approved = run.approved
    rejected = run.rejected

    console.print(
        f"Approved: [green]{len(approved)}[/green]  Rejected: [red]{len(rejected)}[/red]  Pending: {len(run.pending)}"
    )

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
        console.print(
            f"\n[dim]{len(approved)} approved changes ready. Use --push to write to Shopify.[/dim]"
        )

    # Collect and save feedback (from ALL dispositions, not just applied)
    feedback_entries = collect_feedback(run)
    if feedback_entries:
        feedback_dir = Path(run_dir) / "feedback"
        save_feedback(feedback_entries, feedback_dir)
        console.print(f"\nFeedback saved: {len(feedback_entries)} entries to {feedback_dir}")

        # --- Feedback analysis ---
        decisions_path = Path(run_dir) / "match_decisions.jsonl"
        if decisions_path.exists():
            from lookout.feedback.analyzer import analyze
            from lookout.feedback.replay import replay_proposal
            from lookout.feedback.report import format_report, format_terminal, write_report

            clusters = analyze(decisions_path, feedback_dir)
            if clusters:
                # Replay each proposal to get impact diffs
                diffs = []
                for c in clusters:
                    if c.proposal:
                        diffs.append(replay_proposal(c.proposal, decisions_path))

                # Count totals from decision log
                import json

                all_decisions = [
                    json.loads(ln) for ln in decisions_path.read_text().splitlines() if ln.strip()
                ]
                total = len(all_decisions)
                rejected = sum(
                    1 for d in all_decisions if d.get("outcome") in ("no_match", "all_failed")
                )

                # Terminal summary
                report_path = Path(run_dir) / "feedback_analysis.md"
                console.print(format_terminal(clusters, diffs, total, rejected, report_path))

                # Full report to file
                full_report = format_report(clusters, diffs, total, rejected)
                write_report(full_report, Path(run_dir))


@enrich.command("revert")
@click.option(
    "--handle", "-h", "handles", multiple=True, help="Product handles to revert (repeatable)"
)
@click.option(
    "--run-dir",
    "-d",
    type=click.Path(exists=True, path_type=Path),
    help="Revert all products from a run",
)
@click.option(
    "--backup-dir",
    type=click.Path(exists=True, path_type=Path),
    default=Path("./backups"),
    help="Directory containing backups",
)
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
@click.option(
    "--feedback-dir",
    "-d",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Feedback directory to summarize",
)
@click.option("--all-runs", is_flag=True, help="Aggregate feedback across all campaign runs")
@click.option("--verbose", is_flag=True)
def feedback_cmd(feedback_dir, all_runs, verbose):
    """Show feedback summary from review dispositions.

    Displays approval rate, rejection reasons, and trends over time.
    """
    setup_logging(verbose)
    from lookout.feedback.collector import feedback_summary, load_all_feedback

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


@enrich.command("test-resolver")
@click.option(
    "--output-dir",
    "-d",
    type=click.Path(exists=True, path_type=Path),
    default=Path("./output"),
    help="Directory containing match_decisions.jsonl files",
)
@click.option("--verbose", is_flag=True)
def test_resolver_cmd(output_dir, verbose):
    """Replay resolver scoring against cached candidates to detect regressions."""
    import json

    from lookout.enrich.resolver import rescore_candidates
    from lookout.enrich.utils.config import load_vendors_config

    vendors_path = find_vendors_yaml(None)
    if vendors_path:
        vc = load_vendors_config(vendors_path)
    else:
        vc = None

    decisions = []
    for path in output_dir.rglob("match_decisions.jsonl"):
        for line in path.read_text().strip().split("\n"):
            if line.strip():
                decisions.append(json.loads(line))

    if not decisions:
        console.print("[yellow]No match_decisions.jsonl files found[/yellow]")
        return

    testable = [d for d in decisions if d.get("resolver_candidates")]
    skipped = len(decisions) - len(testable)

    passed = 0
    regressed = 0
    regressions = []

    for d in testable:
        domain = ""
        if vc:
            vendor_config = vc.vendors.get(d["vendor"])
            if vendor_config:
                domain = vendor_config.domain

        rescored = rescore_candidates(
            candidates=d["resolver_candidates"],
            product_title=d["catalog_title"],
            vendor=d["vendor"],
            domain=domain,
            catalog_price=d.get("catalog_price"),
        )

        if not rescored:
            continue

        new_winner = rescored[0]["url"]
        original_winner = d.get("final_url")

        if original_winner and new_winner == original_winner:
            passed += 1
        elif original_winner:
            regressed += 1
            regressions.append(
                {
                    "handle": d["handle"],
                    "vendor": d["vendor"],
                    "expected": original_winner,
                    "got": new_winner,
                }
            )
            if verbose:
                console.print(
                    f"[red]REGRESSED[/red] {d['handle']}: expected {original_winner[:50]} got {new_winner[:50]}"
                )
        else:
            if verbose:
                console.print(f"[dim]SKIP[/dim] {d['handle']}: original was no_match")

    console.print("\n[bold]Resolver Regression Results[/bold]")
    console.print(f"  Passed: {passed}")
    console.print(f"  Regressed: {regressed}")
    console.print(f"  Skipped (no cached candidates): {skipped}")
    console.print(f"  Total decisions: {len(decisions)}")

    if regressions:
        console.print("\n[red]Regressions:[/red]")
        for r in regressions:
            console.print(f"  {r['handle']} ({r['vendor']})")
            console.print(f"    expected: {r['expected'][:70]}")
            console.print(f"    got:      {r['got'][:70]}")

    if regressed > 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# enrich push / undo
# ---------------------------------------------------------------------------

_SHOPIFY_CONFIG_PATH = Path.home() / ".tvr" / "shopify" / "config.json"


@enrich.command("push")
@click.option(
    "--run-dir",
    "-d",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Enrichment output directory (for description changes)",
)
@click.option(
    "--dispositions",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="Dispositions JSON with approved_matches",
)
@click.option("--dry-run", is_flag=True, help="Preview without writing to Shopify")
@click.option("--verbose", is_flag=True)
def push_cmd(run_dir, dispositions, dry_run, verbose):
    """Push approved enrichment output (images + descriptions) to Shopify."""
    import json
    from collections import defaultdict
    from datetime import UTC, datetime

    import httpx

    from lookout.push.manifest import PushManifest, PushSummary, save_manifest
    from lookout.push.pusher import ShopifyPusher

    setup_logging(verbose)

    with open(_SHOPIFY_CONFIG_PATH) as f:
        config = json.load(f)

    with open(dispositions) as f:
        disp_data = json.load(f)

    approved = disp_data.get("approved_matches", [])
    if not approved:
        console.print("[red]No approved_matches found in dispositions file[/red]")
        return

    # Group assignments by handle
    by_handle: dict[str, list[dict]] = defaultdict(list)
    for match in approved:
        by_handle[match["handle"]].append(match)

    # Load description changes from run_dir if provided
    body_html_by_handle: dict[str, str] = {}
    if run_dir:
        for merch_dir in run_dir.iterdir():
            if not merch_dir.is_dir():
                continue
            merch_output = merch_dir / "merch_output.json"
            if merch_output.exists():
                data = json.loads(merch_output.read_text())
                if data.get("body_html"):
                    body_html_by_handle[data["handle"]] = data["body_html"]

    console.print(
        f"{'[yellow]DRY RUN[/yellow] ' if dry_run else ''}"
        f"Pushing {len(approved)} image assignments across {len(by_handle)} products"
    )
    if body_html_by_handle:
        console.print(f"  + {len(body_html_by_handle)} description updates")

    from lookout.store import _default_db_url

    db_url = _default_db_url()
    pusher = ShopifyPusher(config=config, db_url=db_url, dry_run=dry_run)
    run_id = run_dir.name if run_dir else "manual"

    # --- Dolt checkpoint (skip for dry-run) ---
    if not dry_run:
        from lookout.push.checkpoint import CheckpointError, create_dolt_checkpoint

        try:
            tag = create_dolt_checkpoint(db_url, run_id)
            console.print(f"Dolt checkpoint: [green]{tag}[/green]")
        except CheckpointError as e:
            console.print(f"[red]Checkpoint failed: {e}[/red]")
            console.print("[red]Aborting push — cannot create safety checkpoint[/red]")
            return
    else:
        console.print("[yellow]DRY RUN — skipping Dolt checkpoint[/yellow]")

    products_manifest: dict = {}
    summary = {
        "pushed": 0,
        "skipped": 0,
        "images_created": 0,
        "images_skipped": 0,
        "descriptions_updated": 0,
        "failed": 0,
    }

    async def _run():
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, (handle, assignments) in enumerate(sorted(by_handle.items())):
                body_html = body_html_by_handle.get(handle)
                try:
                    pm = await pusher.push_product(handle, assignments, client, body_html)
                    if pm.product_id == 0:
                        summary["skipped"] += 1
                        continue
                    products_manifest[handle] = pm
                    summary["pushed"] += 1
                    summary["images_created"] += len(pm.pushed.images_created)
                    if pm.pushed.body_html is not None:
                        summary["descriptions_updated"] += 1
                except Exception as e:
                    console.print(f"[red]Failed: {handle}: {e}[/red]")
                    summary["failed"] += 1

                if (i + 1) % 25 == 0:
                    console.print(f"  Progress: {i + 1}/{len(by_handle)}")

        pusher.close()

    asyncio.run(_run())

    # Save manifest
    manifest = PushManifest(
        run_id=run_id,
        pushed_at=datetime.now(UTC),
        dispositions_path=str(dispositions),
        summary=PushSummary(
            products_pushed=summary["pushed"],
            images_created=summary["images_created"],
            images_skipped=summary["images_skipped"],
            descriptions_updated=summary["descriptions_updated"],
            failed=summary["failed"],
        ),
        products=products_manifest,
    )

    output_dir = run_dir.parent if run_dir else Path("output")
    manifest_path = save_manifest(manifest, output_dir)

    # Print summary
    table = Table(title="Push Summary")
    table.add_column("Metric")
    table.add_column("Count", style="cyan")
    table.add_row("Products pushed", str(summary["pushed"]))
    table.add_row("Images created", str(summary["images_created"]))
    table.add_row("Descriptions updated", str(summary["descriptions_updated"]))
    table.add_row("Skipped", str(summary["skipped"]))
    table.add_row("Failed", str(summary["failed"]))
    console.print(table)
    console.print(f"\nManifest saved: [green]{manifest_path}[/green]")
    console.print("Use [cyan]lookout enrich undo --manifest <path>[/cyan] to revert")


@enrich.command("undo")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="Push manifest JSON from a previous push run",
)
@click.option(
    "--handle",
    "-h",
    "handles",
    multiple=True,
    help="Only undo specific product handles (repeatable)",
)
@click.option(
    "--confirm", is_flag=True, help="Actually execute the undo (default is dry-run preview)"
)
@click.option("--verbose", is_flag=True)
def undo_cmd(manifest_path, handles, confirm, verbose):
    """Undo a previous push using its manifest. Dry-run by default."""
    import json

    from lookout.push.manifest import load_manifest
    from lookout.push.undo import PushUndoer

    setup_logging(verbose)

    with open(_SHOPIFY_CONFIG_PATH) as f:
        config = json.load(f)

    manifest = load_manifest(manifest_path)
    handle_list = list(handles) if handles else None

    target_products = manifest.products
    if handle_list:
        target_products = {h: m for h, m in manifest.products.items() if h in handle_list}
        missing = set(handle_list) - set(target_products.keys())
        if missing:
            console.print(f"[yellow]Handles not in manifest: {missing}[/yellow]")

    total_images = sum(len(pm.pushed.images_created) for pm in target_products.values())
    total_body = sum(1 for pm in target_products.values() if pm.pushed.body_html is not None)

    console.print(f"Manifest: [cyan]{manifest.run_id}[/cyan] (pushed {manifest.pushed_at})")
    console.print(f"Products to undo: {len(target_products)}")
    console.print(f"Images to delete: {total_images}")
    if total_body:
        console.print(f"Descriptions to restore: {total_body}")

    if not confirm:
        console.print("\n[yellow]DRY RUN — add --confirm to execute[/yellow]")

    dry_run = not confirm
    undoer = PushUndoer(config=config, dry_run=dry_run)

    result = asyncio.run(undoer.undo_run(manifest, handle_list))

    table = Table(title="Undo Summary")
    table.add_column("Metric")
    table.add_column("Count", style="cyan")
    table.add_row("Products undone", str(result["products_undone"]))
    table.add_row("Images deleted", str(result["images_deleted"]))
    table.add_row("Variant assignments restored", str(result["variant_assignments_restored"]))
    table.add_row("Descriptions restored", str(result["body_restored"]))
    if result["errors"]:
        table.add_row("Errors", str(len(result["errors"])))
    console.print(table)

    if result["errors"]:
        console.print("\n[red]Errors:[/red]")
        for err in result["errors"]:
            console.print(f"  {err}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
