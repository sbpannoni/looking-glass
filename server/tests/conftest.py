"""Keep the deployment's own environment out of the test process.

`/etc/looking-glass/env` is the service's EnvironmentFile, and sourcing it into
a shell before running pytest is a natural thing to do -- it is how you drive
the live API by hand. Do it and the suite goes from 1 failure to 21, which
reads like the change under test broke everything.

The mechanism is narrow. Two of those variables are auth GATES rather than
credentials: when `LOOKING_GLASS_HUD_TOKEN` (or `API_SERVER_KEY`) is set the
server starts requiring it, and every `TestClient` request -- which sends no
token header -- gets a 401 instead of the response the test asserts on. The
tests are not wrong and the gate is not wrong; they just cannot both be true
in one process.

So those two are stripped, and nothing else is. The rest of that file is
credentials (`GITHUB_TODO_TOKEN`, `HASS_TOKEN`, `HERMES_DASHBOARD_*`), which
gate no requests and are exactly what the opt-in live integration tests key
off -- test_api_projects_fetches_real_repos_with_real_token skips itself
without a real `GITHUB_TODO_TOKEN`. Clearing those would not fix a failure, it
would silently turn the live tests into permanent skips, which is a worse
outcome than the noise being fixed here.

Set LOOKING_GLASS_TEST_KEEP_AUTH_ENV=1 to keep them, for deliberately
exercising the gate itself.
"""

import os

import pytest

# Read per-request by the HUD gate and the API-key check, so their mere
# presence changes every response the TestClient sees.
AUTH_GATE_ENV = ("LOOKING_GLASS_HUD_TOKEN", "API_SERVER_KEY")


@pytest.fixture(scope="session", autouse=True)
def _strip_auth_gate_env():
    """Drop the gate variables for the whole session.

    Session-scoped and autouse rather than a module-level `del`: server.py
    calls load_env() at import, which re-populates from `~/.hermes/.env` or
    `<repo>/.env` if either exists. Running after collection -- so after that
    import -- catches the file case as well as the inherited-shell case.
    """
    if os.environ.get("LOOKING_GLASS_TEST_KEEP_AUTH_ENV"):
        yield
        return
    saved = {name: os.environ.pop(name) for name in AUTH_GATE_ENV
             if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
