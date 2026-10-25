"""Crash-diagnostic text helpers for the kanban dispatcher (t_57e2a3df move).

Error fingerprints and worker-log scrubbing strings, split out of
``kanban_db_dispatch`` (file-length ratchet; see AGENTS.md "offset the
growth"): moved code keeps its caps, callers import from here.
"""
from __future__ import annotations

import re


def error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


def exit_summary_marker() -> str:
    """The CLI exit-summary header (``cli_session_mixin.show_exit_summary``), in the active language."""
    from agent.i18n import t
    return t("cli.session.exit_resume_hint")


def log_noise_prefixes() -> tuple[str, ...]:
    from agent.i18n import t
    return ("session_id:", "Query:", t("cli.chat.initializing_agent"))
