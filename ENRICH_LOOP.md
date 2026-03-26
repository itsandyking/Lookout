# Lookout Enrichment Campaign Loop

You are running an iterative enrichment campaign for TMA's Shopify store. Each iteration you process a batch of products that need content (descriptions, images, variant images).

## Environment

- Working directory: /Users/andyking/Lookout
- Load API keys: `set -a && source .env && set +a`
- CLI: `uv run lookout`
- Run state is tracked in `campaign/` directory

## Cost Rates

| Resource | Rate |
|---|---|
| Brave Search | $0.005/query ($5/1000, $5 free credit/month) |
| Anthropic (Sonnet, description) | ~$0.005/product (1.5K tokens avg) |
| Anthropic (Sonnet, variant images) | ~$0.008/product (2K tokens avg) |
| Playwright scrape | $0 (local) |

**Efficiency metric:** Cost per product at confidence level. Track `cost_per_product` and `avg_confidence` each iteration. Lower cost + higher confidence = better. Products requiring retries are expensive — flag patterns that cause retries so future batches avoid them.

## Each Iteration

### 1. Check campaign state

Read `campaign/state.json` to see what's been done. Check costs against budget ceiling.

### 2. Check budget

**Hard limits per session:**
- Brave Search: 30 queries max ($0.15)
- Anthropic LLM: 15 calls max (~$0.10)
- Total session budget: $0.50

If approaching limits, reduce batch size or stop. Output cost summary before continuing.

### 3. Prepare the next batch

If no input CSV exists yet at `campaign/next_batch.csv`, create one. Options:
- If the database is available: `uv run lookout audit --vendor <VENDOR> --out campaign/next_batch.csv`
- If no database: use a hand-crafted CSV with products from configured vendors (check vendors.yaml)

Batch size: 3-5 products per iteration.

### 4. Run enrichment

```bash
set -a && source .env && set +a
uv run lookout enrich run -i campaign/next_batch.csv --out campaign/run_<iteration>/ --max-rows 3
```

### 5. Review results and track costs

Read the run report CSV. For each product, calculate cost:
- Count Brave queries from resolver.json (each `query_used` entry ≈ 1 query, broad validation ≈ 1 extra)
- Count LLM calls (1 for description if generated, 1 for variant images if Tier 2)
- Calculate: `product_cost = (brave_queries × $0.005) + (llm_calls × $0.006)`

Assess each product:
- **UPDATED with confidence >= 85**: Success. Log cost and confidence.
- **UPDATED with confidence 70-84**: Review quality. Note if cost was high (retries).
- **NO_MATCH**: URL resolution failed. Note the Brave queries spent for nothing.
- **FAILED**: Check error. Note wasted API costs.

**Efficiency score per product:** `confidence / (cost × 100)`. Higher is better.
- Score > 500: excellent (high confidence, low cost)
- Score 100-500: good
- Score < 100: poor (low confidence or high cost — investigate)

### 6. Handle failures

For NO_MATCH or FAILED products:
- Check the resolver.json artifact for what was tried
- Consider: is the handle wrong? Is the vendor site blocking us? Is the product discontinued?
- If retryable AND efficiency would likely improve: add to `campaign/retry_batch.csv`
- If retry would just burn more budget for low confidence: skip and log reason
- **Rule: don't retry a product more than twice.** Three strikes = human review queue.

### 7. Update campaign state

Update `campaign/state.json` with:

```json
{
  "iteration": N,
  "total_processed": X,
  "total_updated": Y,
  "total_failed": Z,
  "total_no_match": W,
  "costs": {
    "brave_queries": N,
    "brave_cost": 0.00,
    "llm_calls": N,
    "llm_cost": 0.00,
    "total_cost": 0.00,
    "cost_per_product": 0.00,
    "avg_confidence": 0,
    "efficiency_score": 0
  },
  "products_completed": [...],
  "products_retry": [...],
  "products_review": [...],
  "vendor_stats": {
    "Vendor Name": {
      "processed": N,
      "updated": N,
      "avg_confidence": N,
      "avg_cost": 0.00,
      "efficiency_score": N
    }
  }
}
```

### 8. Decide next action

- If budget remaining and retries queued: process retries
- If budget remaining and current vendor done: move to next vendor
- If budget ceiling hit: output summary and stop
- If all vendors done or all batches processed: stop

### 9. Output iteration summary

```
## Iteration N Summary
- Processed: X products
- Updated: Y (avg confidence: Z%)
- Failed/No Match: A
- Iteration cost: $X.XX (Brave: $X.XX, LLM: $X.XX)
- Cost per product: $X.XX
- Efficiency score: X (confidence / cost)
- Budget remaining: $X.XX of $0.50
- Campaign totals: M products, $X.XX spent, avg efficiency: X
```

## Vendor Performance Tracking

Track per-vendor stats to identify which vendors are easy (high confidence, low cost) vs. hard (low confidence, many retries). This informs which vendors to prioritize in future campaigns.

Good vendors: simple HTML, JSON-LD product data, color variants in structured data
Hard vendors: JS configurators, bot protection, non-standard URL patterns
