"""`kanban edit --skills/--clear-skills` — official edit surface for tasks.skills.

Every expectation here comes from the requested CLI surface or from the
existing ``create --skill`` acceptance text on main (``_normalize_task_skills``
in kanban_db.py) — not from the edit implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _skills_column(task_id: str):
    """Raw tasks.skills cell — NULL vs value is the storage contract."""
    with kbc.connect_closing() as conn:
        return conn.execute("SELECT skills FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def test_kanban_edit_skills_repeated_and_space_separated_read_back(kanban_home):
    """`edit --skills a --skills b` and `edit --skills a b` both land in
    tasks.skills and `get`/Task.skills reads the same list back (spec: 寫入後
    get 回讀一致; space-separated + repeatable accepted)."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="skills edit")

    out = kc.run_slash(f"edit {task_id} --skills translation --skills github-code-review")
    assert "Edited" in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).skills == ["translation", "github-code-review"]

    out = kc.run_slash(f"edit {task_id} --skills blogwatcher jupyter-live-kernel")
    assert "Edited" in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).skills == ["blogwatcher", "jupyter-live-kernel"]
    assert _skills_column(task_id) == '["blogwatcher", "jupyter-live-kernel"]'


def test_kanban_edit_clear_skills_stores_null(kanban_home):
    """`edit --clear-skills` writes NULL (spec: --clear-skills 後為 NULL)."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="clear me", skills=["translation"])
    assert _skills_column(task_id) == '["translation"]'

    out = kc.run_slash(f"edit {task_id} --clear-skills")
    assert "Edited" in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).skills is None
    assert _skills_column(task_id) is None


def test_kanban_edit_skills_rejects_comma_with_create_message(kanban_home):
    """Comma rejection text on edit equals create's (spec: 拒絕訊息與 create
    一致) — both are `_normalize_task_skills`'s ValueError via the same
    run_slash error path."""
    with kbc.connect_closing() as conn:
        t_edit = kb.create_task(conn, title="edit lane")

    create_out = kc.run_slash("create create-lane --skill alpha,beta")
    edit_out = kc.run_slash(f"edit {t_edit} --skills alpha,beta")

    # "與 create 一致" asserted exactly: neither error text carries the task
    # id or title, so the two rejection paths must print byte-identical text.
    assert "skill name cannot contain comma" in create_out, create_out
    assert create_out == edit_out
    # refusal must not touch the stored value
    assert _skills_column(t_edit) is None


def test_kanban_edit_skills_rejects_toolset_name_with_create_message(kanban_home):
    """Toolset-name rejection text on edit equals create's (spec: 拒絕訊息與
    create 一致)."""
    with kbc.connect_closing() as conn:
        t_edit = kb.create_task(conn, title="toolset lane")

    create_out = kc.run_slash("create toolset-lane --skill web")
    edit_out = kc.run_slash(f"edit {t_edit} --skills web")

    assert "is a toolset name, not skill name" in create_out, create_out
    assert create_out == edit_out
    assert _skills_column(t_edit) is None


def test_kanban_edit_skills_and_clear_skills_are_mutually_exclusive(kanban_home):
    """`--skills` together with `--clear-skills` is a usage error; the stored
    value stays untouched (spec: 同給報錯)."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="mutex", skills=["translation"])

    out = kc.run_slash(f"edit {task_id} --skills blogwatcher --clear-skills")

    assert "mutually exclusive" in out, out
    assert _skills_column(task_id) == '["translation"]'


def test_kanban_edit_skills_alone_passes_the_field_guard(kanban_home):
    """`--skills` alone satisfies the provide-a-field guard (the old guard
    only accepted --title/--body/--priority/--result)."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="guard")

    out = kc.run_slash(f"edit {task_id} --skills translation")

    assert "provide --title" not in out, out
    assert "Edited" in out, out
