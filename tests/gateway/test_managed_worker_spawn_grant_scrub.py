"""Managed-worker spawn is a grant boundary (t_6be16389 / D4): the child env never carries
HERMES_DELEGATED_CHILD_CONTEXT, matching the CLI dispatcher precedent
(hermes_cli.kanban_db_dispatch pops the marker before worker spawn). Only the marker is
scrubbed; the kanban pins ride through untouched (board-isolation fence).

Expectations come from the card spec (需求單 t_791a8d5b D4 verify 條):
「父環境含 marker 時，產出環境不含 marker 且 kanban pins 齊備」.
"""
from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

PINS = {
    "HERMES_KANBAN_DB": "/tmp/board/kanban.db",
    "HERMES_KANBAN_WORKSPACES_ROOT": "/tmp/board/workspaces",
    "HERMES_KANBAN_BOARD": "fund13",
}


def test_inherited_env_scrubs_marker_and_keeps_pins(tmp_path, monkeypatch):
    """None env (single-profile byte-for-byte inheritance) with a dirty launch env:
    the worker gets os.environ minus the marker, pins intact."""
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, str(tmp_path / ".hermes"))
    for k, v in PINS.items():
        monkeypatch.setenv(k, v)
    from gateway.session_managed_worker import _strip_delegated_child_marker
    env = _strip_delegated_child_marker(None)
    assert env is not None
    assert DELEGATED_CHILD_ENV_MARKER not in env
    assert {k: env[k] for k in PINS} == PINS


def test_constructed_env_scrubs_marker_and_keeps_pins(tmp_path):
    """Routed/multiplex-constructed dict carrying the marker: only the marker is popped."""
    from gateway.session_managed_worker import _strip_delegated_child_marker
    env = {DELEGATED_CHILD_ENV_MARKER: str(tmp_path / ".hermes"), **PINS, "PATH": "/usr/bin"}
    out = _strip_delegated_child_marker(env)
    assert DELEGATED_CHILD_ENV_MARKER not in out
    assert {k: out[k] for k in PINS} == PINS
    assert out["PATH"] == "/usr/bin"


def test_clean_env_keeps_none_byte_for_byte_inheritance(monkeypatch):
    """No marker anywhere: None is preserved so the common path keeps exact Popen(env=None)
    inheritance (the single-profile 'byte-for-byte' rule in _worker_env's contract)."""
    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    from gateway.session_managed_worker import _strip_delegated_child_marker
    assert _strip_delegated_child_marker(None) is None
