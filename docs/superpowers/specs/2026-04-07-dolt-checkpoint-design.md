# Dolt Checkpoint Before Push — Design

**Date:** 2026-04-07

## Overview

Before `lookout enrich push` mutates anything in Shopify, create a Dolt commit + tag on Pi5 as a revertible checkpoint. If the checkpoint fails, abort the push entirely.

## Motivation

TVR on Pi5 livesyncs Shopify data into Dolt via webhooks. Dolt commits provide point-in-time snapshots. By tagging the current state before a Lookout push, we get a known-good baseline to revert to if a push goes wrong — covering not just what Lookout pushed (manifest handles that) but the full database state including webhook-driven changes.

## Implementation

### Checkpoint function

A single function in `lookout/push/checkpoint.py`:

```
create_dolt_checkpoint(db_url: str, run_id: str) -> str
```

1. Opens a SQLAlchemy connection using the existing `db_url` (same as `load_dolt_config().connection_string` — MySQL wire protocol to Pi5 Dolt server at `pi5:3306`)
2. Executes `CALL dolt_commit('-Am', 'lookout: pre-push checkpoint {run_id}')` — `-A` stages all uncommitted changes, `-m` sets the message
3. Executes `CALL dolt_tag('pre-push/{run_id}_{HHMMSS}', 'HEAD')` — creates a named tag. Timestamp suffix prevents conflicts on re-runs.
4. Returns the tag name on success
5. Raises `CheckpointError` on any failure (connection error, SQL error)

### Integration with push workflow

In the `lookout enrich push` CLI command, before entering the product push loop:

- If `--dry-run`: skip checkpoint (no point for a dry run)
- Otherwise: call `create_dolt_checkpoint()`. If it raises `CheckpointError`, print the error and abort. No Shopify mutations happen.

One checkpoint per push run, not per product.

### Tag naming

Format: `pre-push/{run_id}_{HHMMSS}`

Example: `pre-push/enrich-20260406_143022`

The timestamp suffix handles the case where the same `run_id` is pushed multiple times.

### File placement

`lookout/push/checkpoint.py` — small module (~40 lines) with one public function and a custom exception. Isolated from the pusher and testable independently.

### Error handling

- Connection failure to Pi5 → `CheckpointError` → push aborts
- `dolt_commit` fails (e.g., nothing to commit) → swallow the "nothing to commit" case (the data is already committed by livesync), proceed to tagging
- `dolt_tag` fails → `CheckpointError` → push aborts
- All errors print a clear message explaining that the push was aborted because the checkpoint failed

### Testing

- Mock SQLAlchemy connection, verify correct SQL calls
- Test "nothing to commit" is handled gracefully
- Test connection failure raises `CheckpointError`
- Test tag conflict handling
- Test dry-run skips checkpoint
- Test push CLI aborts on checkpoint failure

## Out of Scope

- Standalone `lookout checkpoint` CLI command (not needed if automatic)
- Automatic revert-to-checkpoint after failed push (manual decision)
- Checkpoint for non-push operations (audit, enrich run)
