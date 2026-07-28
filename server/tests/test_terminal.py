import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402


def test_all_seven_fleet_hosts_are_allowed():
    for name in ("snarf", "r720", "octominer", "beelink", "claude-control", "hermes", "looking-glass"):
        assert srv.is_allowed_terminal_host(name), f"{name} should be allowed"


def test_unknown_host_is_rejected():
    assert not srv.is_allowed_terminal_host("not-a-real-host")
    assert not srv.is_allowed_terminal_host("")
    assert not srv.is_allowed_terminal_host("192.168.1.1")  # raw IPs not accepted, only known names


def test_terminal_hosts_have_required_fields():
    for name, spec in srv.TERMINAL_HOSTS.items():
        assert "host" in spec, f"{name} missing 'host'"
        assert "user" in spec, f"{name} missing 'user'"


from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

client = TestClient(srv.app)
TEST_TOKEN = "test-terminal-token"


@pytest.fixture
def with_hud_token(monkeypatch):
    monkeypatch.setenv("LOOKING_GLASS_HUD_TOKEN", TEST_TOKEN)


def test_terminal_ws_rejects_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("LOOKING_GLASS_HUD_TOKEN", raising=False)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/terminal/snarf"):
            pass
    assert exc_info.value.code == 4401


def test_terminal_ws_rejects_missing_token_even_without_origin(with_hud_token):
    """The gap the security review caught: _ws_allowed() alone exempts
    no-Origin (non-browser) callers from the token check entirely. The
    terminal route must require the token unconditionally regardless."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/terminal/snarf"):  # no ?token=, no Origin header
            pass
    assert exc_info.value.code == 4401


def test_terminal_ws_rejects_wrong_token(with_hud_token):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/terminal/snarf?token=wrong-token"):
            pass
    assert exc_info.value.code == 4401


def test_terminal_ws_rejects_unknown_host(with_hud_token):
    # server closes before ever accepting -> TestClient raises on __enter__,
    # not something read via a subsequent .receive() call
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/terminal/not-a-real-host?token={TEST_TOKEN}"):
            pass
    assert exc_info.value.code == 4404


def test_terminal_ws_rejects_disallowed_origin(with_hud_token, monkeypatch):
    monkeypatch.setattr(srv, "ALLOWED_ORIGIN_HOSTS", {"looking-glass.local"})
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            f"/ws/terminal/snarf?token={TEST_TOKEN}", headers={"origin": "https://evil.example"}
        ):
            pass
    assert exc_info.value.code == 4401


def test_terminal_ws_runs_a_real_command_on_snarf(with_hud_token):
    """Live integration test — requires Task 8's key deployment to be done
    and snarf reachable. Not mocked: proves the real SSH path works."""
    with client.websocket_connect(f"/ws/terminal/snarf?token={TEST_TOKEN}") as ws:
        ws.send_text("hostname\n")
        output = ""
        for _ in range(20):  # accumulate a few frames, PTY output can be chunked
            output += ws.receive_text()
            if "snarf" in output:
                break
        assert "snarf" in output
