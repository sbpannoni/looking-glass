import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402


def test_parse_checkbox_md_counts_done_and_open():
    text = """# TODO
- [ ] first open item
- [x] a done item
- [X] a done item, uppercase X
  - [ ] a nested/indented open item
- not a checkbox line at all
"""
    result = srv._parse_checkbox_md(text)
    assert result["done"] == 2
    assert result["open"] == 2
    assert result["total"] == 4


def test_parse_checkbox_md_empty_file():
    result = srv._parse_checkbox_md("# TODO\n\nnothing here yet\n")
    assert result == {"done": 0, "open": 0, "total": 0}


def test_parse_status_table_md_counts_by_emoji():
    text = """## SNARF
| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Rack shelf install | ✅ | done |
| 2 | HBA config | 🚧 | blocked on cables |
| 3 | ZFS pool | 🔄 | in progress |
| 4 | Test DIMM | 📋 | not started |

## R720
| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Something else | ✅ | done too |
"""
    result = srv._parse_status_table_md(text)
    assert result["done"] == 2
    assert result["blocked"] == 1
    assert result["in_progress"] == 1
    assert result["todo"] == 1
    assert result["total"] == 5
    assert result["open"] == 3


def test_parse_status_table_md_ignores_separator_and_header_rows():
    text = "| # | Task | Status | Notes |\n|---|---|---|---|\n"
    result = srv._parse_status_table_md(text)
    assert result["total"] == 0


from fastapi.testclient import TestClient

client = TestClient(srv.app)


def test_api_projects_requires_auth(monkeypatch):
    monkeypatch.setenv("LOOKING_GLASS_HUD_TOKEN", "test-token")
    resp = client.get("/api/projects")
    assert resp.status_code == 401


def test_api_projects_returns_error_entries_without_github_token(monkeypatch):
    monkeypatch.delenv("LOOKING_GLASS_HUD_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TODO_TOKEN", raising=False)
    srv.PROJECTS_CACHE.update(ts=0.0, data={})  # bust the cache for a clean test
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["projects"]) == 4
    assert all(p.get("error") == "unreachable" for p in data["projects"])


def test_api_projects_fetches_real_repos_with_real_token():
    """Live integration test — requires GITHUB_TODO_TOKEN to actually be set
    in the environment (the real deployed token, not a fake one)."""
    import os
    if not os.environ.get("GITHUB_TODO_TOKEN"):
        import pytest
        pytest.skip("GITHUB_TODO_TOKEN not set in this environment")
    srv.PROJECTS_CACHE.update(ts=0.0, data={})
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    names = {p["name"] for p in data["projects"]}
    assert names == {"my-website", "DARKHELIX", "redqueen-website", "server"}
    for p in data["projects"]:
        assert "error" not in p, f"{p['name']} failed to fetch: {p}"
        assert "total" in p
