import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as srv  # noqa: E402


def test_all_seven_fleet_hosts_are_allowed():
    for name in ("snarf", "r720", "octominer", "beelink", "claude-control", "hermes", "jarvis-hud"):
        assert srv.is_allowed_terminal_host(name), f"{name} should be allowed"


def test_unknown_host_is_rejected():
    assert not srv.is_allowed_terminal_host("not-a-real-host")
    assert not srv.is_allowed_terminal_host("")
    assert not srv.is_allowed_terminal_host("192.168.1.1")  # raw IPs not accepted, only known names


def test_terminal_hosts_have_required_fields():
    for name, spec in srv.TERMINAL_HOSTS.items():
        assert "host" in spec, f"{name} missing 'host'"
        assert "user" in spec, f"{name} missing 'user'"
