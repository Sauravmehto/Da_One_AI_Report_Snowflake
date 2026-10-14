"""Test environment, set before any test module imports the app.

`app.core.config.settings` is a module-level singleton built once, at first
import of app.core.config. Anything that wants to influence it via environment
variables therefore has to set them BEFORE that import happens.

Individual test modules used to do this at their own import time, which worked
only while they happened to be the first module pytest imported — an ordering
dependency that silently broke as soon as an alphabetically-earlier test file
was added. conftest.py is loaded before any test module, so this is the one
place the ordering is guaranteed.

Values are deliberately fake: tests must never reach a real Snowflake account
or OAuth endpoint.
"""

import os

# Assigned, not setdefault. pipenv exports backend/.env before pytest starts,
# and an empty OAUTH_CLIENT_ID= counts as set, so setdefault would leave the
# login route returning 503. These values stay fake even when a developer has
# copied .env.example.
os.environ["SESSION_BACKEND"] = "memory"
# Tests must not write conversation files to disk.
os.environ["CONVERSATION_STORE"] = "memory"
os.environ["SESSION_STORE"] = "memory"
os.environ["JWT_SECRET"] = "test-secret"

# OAuth config — the auth-route tests assert /auth/login redirects to Snowflake,
# which only happens when a client id and account are configured.
os.environ["OAUTH_CLIENT_ID"] = "test-client"
os.environ["OAUTH_CLIENT_SECRET"] = "test-secret"
os.environ["SNOWFLAKE_ACCOUNT"] = "test-account"
os.environ["SNOWFLAKE_WAREHOUSE"] = "TEST_WH"

# The engine's Snowflake client reads these at import time.
os.environ["SNOWFLAKE_USER"] = "test-user"

if not os.environ.get("TOKEN_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet

    os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
