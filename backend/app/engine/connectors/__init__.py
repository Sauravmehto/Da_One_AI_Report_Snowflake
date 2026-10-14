"""Vendor-agnostic data-source connectors.

Each subpackage (connectors/snowflake/, and future ones like
connectors/sqlserver/) is self-contained: its own client/auth/dialect
concerns live entirely inside its own folder and it registers itself with
`connectors.registry` on import. Nothing in this top-level package, in
connectors/base.py, or in any OTHER connector should ever need to change to
add a new vendor.
"""
