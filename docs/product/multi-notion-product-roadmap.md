# Multi-Notion Product Roadmap

## Context

The current repo is a strong prototype for one narrow job: route Notion reads
and basic writes across two explicit workspace slots without accidentally
writing to the wrong org.

Today it can:

- validate two configured workspace connections
- search pages and fetch pages/databases
- query databases
- create pages
- append blocks
- optionally support a later OAuth-based connect flow

That is enough for a safe dual-workspace MCP bridge, but it is not yet the
full product surface of a multi-Notion connection product.

## Objective

Expand the repo from a dual-slot integration helper into a reusable multi-Notion
platform that can:

- connect many Notion workspaces safely
- expose a complete day-to-day operator surface for each workspace
- support cross-workspace workflows such as copy, sync, and compare
- enforce safety controls that reduce accidental cross-posting or destructive writes
- feel like a real product rather than a one-off local bridge

## Non-Goals

- Build every Notion endpoint immediately
- Make OAuth a blocker for the broader product surface
- Hide workspace routing in ways that increase the risk of writes landing in the wrong org
- Attempt a full bidirectional sync engine before the single-workspace operator surface is stable

## Current Product Shape

The current implementation is best described as:

- one local MCP server
- two hardcoded workspace slots: `primary` and `secondary`
- explicit workspace selection on every operation
- compact read and write primitives
- local env-based credential storage

That shape is good for safety, but it will not scale cleanly to a broader
multi-workspace product because the connection model, tool registry, and config
storage are still built around exactly two slots.

## Full Product Surface

### 1. Connection Management

The product should support:

- registering more than two workspaces
- naming and renaming connected workspaces
- tracking auth mode, health, capabilities, and last validation time
- reporting permission gaps in human language
- rotating or replacing credentials without manual file surgery

Representative product capabilities:

- `list_connections`
- `add_connection`
- `remove_connection`
- `rename_connection`
- `validate_connection`
- `inspect_connection_permissions`

### 2. Single-Workspace Operator Surface

The product should cover the common practical actions an operator or agent needs
inside one workspace:

- page read, update, archive, restore
- block read, append, replace, and delete/archive-safe operations
- database read, query, schema inspection, and item create/update/archive
- parent discovery and page/database lookup helpers
- clearer property-type helpers to avoid malformed write payloads

Representative product capabilities:

- `update_page`
- `archive_page`
- `restore_page`
- `list_block_children`
- `replace_block_children`
- `create_database_item`
- `update_database_item`
- `archive_database_item`
- `inspect_database_schema`

### 3. Cross-Workspace Workflows

This is the real product differentiator. The product should make it safe to
move knowledge and structured data between workspaces without requiring the user
to handcraft every step.

Representative workflows:

- copy a page from workspace A to workspace B
- clone a template page into another workspace
- mirror a database schema across workspaces
- map fields between different databases
- search across all connected workspaces
- compare similarly named pages or databases across orgs
- preview a sync plan before writing anything

Representative product capabilities:

- `search_all_workspaces`
- `copy_page_between_workspaces`
- `clone_template_to_workspace`
- `compare_pages`
- `compare_database_schemas`
- `plan_database_sync`
- `run_database_sync`

### 4. Safety, Policy, and Admin

A real product needs more than connectivity. It needs controls.

Representative product capabilities:

- dry-run mode for writes and syncs
- workspace-level allow/deny policies
- write confirmation for destructive actions
- audit log entries for every write
- per-workspace capability display
- safe error handling for partial cross-workspace failures
- credential storage that can move beyond plain `.env`

Representative product capabilities:

- `preview_write`
- `preview_sync`
- `get_audit_log`
- `set_workspace_policy`
- `explain_write_risk`

### 5. UX, Packaging, and Distribution

To feel complete, the product should also improve how people discover and use it:

- clearer plugin descriptions and setup docs
- better human-readable workspace status output
- canned workflows for common tasks
- examples for page copy, database sync, and safe cleanup
- packaging that does not assume a local power user

## Product Requirements

### R1. Multi-Connection Support

The system must support more than two connected Notion workspaces.

Acceptance criteria:

- users can register at least 5 workspaces without changing code
- each workspace has a stable internal ID and human-readable label
- tools can target any registered workspace explicitly

### R2. Safe Targeting

The system must keep destination selection explicit for all writes.

Acceptance criteria:

- every write tool requires a concrete destination workspace
- cross-workspace tools show both source and destination clearly
- destructive operations support preview or confirmation pathways

### R3. Practical Workspace Operations

The system must support the common read/write lifecycle for pages, blocks, and
database items.

Acceptance criteria:

- pages can be created, updated, archived, and restored
- blocks can be read and appended safely
- database items can be queried and updated with schema-aware helpers

### R4. Cross-Workspace Value

The system must provide at least one durable cross-workspace workflow that is
meaningfully better than manual copying.

Acceptance criteria:

- users can copy a page between workspaces
- users can preview a database sync plan before execution
- schema mismatches are surfaced before writes occur

### R5. Operational Safety

The system must explain failures and preserve trust when things go wrong.

Acceptance criteria:

- every write failure includes workspace context and action context
- partial cross-workspace failures are reported explicitly
- write activity can be audited after the fact

## Milestone Plan

## M1. Complete The Single-Workspace Operator Surface

Goal:
Make the current dual-slot system feel operationally complete before broadening
the connection model.

Scope:

- add `update_page`
- add `archive_page` and `restore_page`
- expose block-read helpers
- improve page/database property helpers
- add tests and smoke-test coverage for cleanup flows

Why first:

- it removes the need for one-off scripts
- it closes the most obvious product gap in the current build
- it creates the primitive set needed for later copy/sync workflows

## M2. Replace Fixed Slots With A Workspace Registry

Goal:
Move from `primary`/`secondary` hardcoding to a dynamic workspace registry.

Scope:

- replace `WORKSPACE_KEYS = ("primary", "secondary")`
- move connection storage to a registry model
- support add/remove/list/rename flows
- preserve aliases and explicit targeting

Why second:

- the current architecture does not scale to the intended product
- cross-workspace workflows become awkward if connections are still hardcoded

## M3. Add Cross-Workspace Read Flows

Goal:
Let users inspect and compare across workspaces before attempting sync.

Scope:

- add `search_all_workspaces`
- add compare helpers for pages and schemas
- add source/destination previews

Why third:

- comparison and preview reduce risk
- these flows are easier to validate before adding write-heavy sync logic

## M4. Add Cross-Workspace Write Flows

Goal:
Ship the first truly differentiated product workflows.

Scope:

- copy page across workspaces
- clone template page into another workspace
- plan and run basic database syncs
- add mapping rules for schema mismatches

Why fourth:

- this is the first step from utility to product
- it depends on stable primitives from M1-M3

## M5. Add Safety, Audit, And Admin Controls

Goal:
Make the product trustworthy for repeated use.

Scope:

- audit logging
- dry-run mode
- policy controls
- clearer permissions diagnostics
- safer secret/config storage

Why fifth:

- these controls matter most once the workflow surface is broad enough to create real risk

## M6. Improve Packaging And Setup

Goal:
Turn the repo into a smoother install/use experience.

Scope:

- refine setup docs and examples
- improve plugin prompt surface
- add more guided workflow examples
- layer OAuth on top when ready

Why sixth:

- packaging polish is most valuable once the core product surface is stable

## Repo Implications

The codebase should evolve from one large server script into a few clearer layers:

- connection registry and config storage
- Notion API client layer
- workspace-scoped read/write tools
- cross-workspace planner and executor tools
- policy and audit layer

The current single-file server was a reasonable starting point. It is not the
right long-term shape for the broader product.

## Recommended Immediate Next Step

Build M1 now.

Specifically:

1. add `archive_page`
2. add `restore_page`
3. add `update_page`
4. expose `list_block_children`
5. add tests and smoke-test coverage for create-update-archive-cleanup flows

That keeps momentum high, removes the need for ad hoc cleanup scripts, and sets
up the repo for the larger jump to dynamic multi-workspace support afterward.
