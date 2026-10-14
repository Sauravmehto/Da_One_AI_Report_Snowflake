"""Reporting engine ported from the standalone MCP server.

Vendor-agnostic orchestration: dashboard planning/persistence/versioning,
LLM SQL generation, the semantic layer, the read-only SQL safety gate, the
glossary, the audit trail, and the Excel-report subsystem.

Two things to know while this is being integrated:

1. **Not yet wired into any route.** The existing app/services/* layer still
   powers the UI unchanged. Nothing here is called from a request path yet,
   which is why it is still synchronous — the sync-vs-async decision belongs
   with the wiring work, not with this port.

2. **Its own Snowflake connection, temporarily.** app.engine.connectors holds
   a module-level, key-pair-authenticated connection inherited from the MCP
   server, separate from app/services/snowflake.py's per-session OAuth
   connections. That duplication is deliberate for now: it keeps the ported
   code behaving exactly as it did under test. Consolidating onto the
   per-session connector is part of the wiring work.

Data access goes through app.engine.connectors.registry.get(<name>), never a
vendor module directly, so adding a new database vendor means adding a folder
under connectors/ and nothing else.
"""
