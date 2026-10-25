"""D1 (t_78e230ae): card-declared ``skills`` must resolve for the ASSIGNEE.

Incident (t_9691bf56, eight consecutive crash-loops): a card carried
``skills=["sdlc-review"]`` while the assignee profile could not resolve that
skill, and nothing refused it until the spawned worker died at startup.
The create/assign entries validate through the shared resolver; the
dispatcher gate is the authority: it must refuse the spawn (no claim, no
admission) and park the card ``blocked``/needs_input naming the skill.
"""
from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

SKILL_X = "alpha-skill"
PROFILE_A = "alpha"   # has SKILL_X installed
PROFILE_B = "beta"    # does NOT have SKILL_X


def _isolated_home(tmp_path, monkeypatch):
    """Tmp HERMES_HOME with two real profile dirs; A ships SKILL_X, B ships nothing."""
    home = tmp_path / ".hermes"
    for prof in (PROFILE_A, PROFILE_B):
        pdir = home / "profiles" / prof
        pdir.mkdir(parents=True)
        # An identity marker is what makes a profiles/<name> dir a live profile.
        (pdir / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill_dir = home / "profiles" / PROFILE_A / "skills" / SKILL_X
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {SKILL_X}\ndescription: test skill\n---\n\nBody.\n",
        encoding="utf-8",
    )
    kb.init_db()
    return home


# --- create entry -----------------------------------------------------------

def test_create_refuses_skill_unresolvable_for_assignee(tmp_path, monkeypatch):
    """RED (t_9691bf56): create with assignee=B skills=[X] must be refused and
    the message must name both the skill and the profile."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        try:
            kb.create_task(conn, title="crash card", assignee=PROFILE_B, skills=[SKILL_X])
            raise AssertionError("create_task accepted an unresolvable skill")
        except ValueError as exc:
            assert SKILL_X in str(exc)
            assert PROFILE_B in str(exc)
        # Nothing half-written for the refused card.
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee = ?", (PROFILE_B,)).fetchone()[0] == 0


def test_create_accepts_skill_the_assignee_can_resolve(tmp_path, monkeypatch):
    """The gate must not become a skills ban: profile A ships SKILL_X, so a
    card assigned to A with skills=[X] is created verbatim."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ok card", assignee=PROFILE_A, skills=[SKILL_X])
        task = kb.get_task(conn, tid)
    assert task is not None and task.skills == [SKILL_X]


def test_assign_refuses_reassign_to_profile_that_cannot_resolve_skills(tmp_path, monkeypatch):
    """RED (t_9691bf56): a card created for A (who has X) reassigned to B must
    be refused and keep its original assignee."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="handoff card", assignee=PROFILE_A, skills=[SKILL_X])
        try:
            kb.assign_task(conn, tid, PROFILE_B)
            raise AssertionError("assign_task accepted an unresolvable skill for the new assignee")
        except ValueError as exc:
            assert SKILL_X in str(exc)
            assert PROFILE_B in str(exc)
        assert kb.get_task(conn, tid).assignee == PROFILE_A
        # The refusal left no 'assigned' event behind (create records 'created').
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "assigned" not in kinds


# --- dispatch authoritative gate --------------------------------------------

def _dirty_card(conn, assignee: str, skills: list[str]) -> str:
    """Bypass create/assign validation the way a cross-host creator does:
    insert the card, then write the skills column directly."""
    tid = kb.create_task(conn, title="dirty card", assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET skills = ? WHERE id = ?",
                     (json.dumps(skills), tid))
    return tid


def test_dispatch_refuses_spawn_and_blocks_on_unresolvable_skill(tmp_path, monkeypatch):
    """RED (t_9691bf56, the authority gate): a ready card whose skill B cannot
    resolve must NOT spawn a worker — the dispatcher parks it blocked with a
    needs_input kind and names the missing skill on the board (event + comment)."""
    _isolated_home(tmp_path, monkeypatch)
    calls: list[str] = []

    def _spy_spawn(task, workspace, *, board=None):
        calls.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _dirty_card(conn, PROFILE_B, [SKILL_X])
        res = kbd.dispatch_once(conn, spawn_fn=_spy_spawn)
        task = kb.get_task(conn, tid)
        events = [(e.kind, e.payload) for e in kb.list_events(conn, tid)]
        comments = [c.body for c in kb.list_comments(conn, tid)]
    assert calls == [], "dispatcher must not spawn for an unresolvable skill"
    assert (tid, [SKILL_X]) in res.blocked_missing_skills
    assert task is not None and task.status == "blocked"
    assert task.block_kind == "needs_input"
    evidence = json.dumps(events) + json.dumps(comments)
    assert SKILL_X in evidence
    assert PROFILE_B in evidence
    # No admission / no worker pid was ever written for the refused spawn.
    assert task.worker_pid is None
    assert not any(k == "owner_admitted" for k, _ in events)


def test_dispatch_spawns_normally_when_skill_resolves_for_assignee(tmp_path, monkeypatch):
    """The same skill card on profile A (which ships SKILL_X) must spawn —
    the gate blocks unresolvable names, not skills."""
    _isolated_home(tmp_path, monkeypatch)
    calls: list[str] = []

    def _spy_spawn(task, workspace, *, board=None):
        calls.append(task.id)
        return 4243

    with kbc.connect() as conn:
        tid = _dirty_card(conn, PROFILE_A, [SKILL_X])
        kbd.dispatch_once(conn, spawn_fn=_spy_spawn)
        task = kb.get_task(conn, tid)
    assert calls == [tid]
    assert task is not None and task.status == "running"


def test_dispatch_dry_run_reports_without_writing(tmp_path, monkeypatch):
    """A dry-run tick names the problem but writes no block and no comment."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        tid = _dirty_card(conn, PROFILE_B, [SKILL_X])
        res = kbd.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert (tid, [SKILL_X]) in res.blocked_missing_skills
    assert task is not None and task.status == "ready"
    assert "blocked" not in kinds


# --- resolver semantics edges -------------------------------------------------

def test_operator_disabled_skill_counts_as_missing(tmp_path, monkeypatch):
    """A skill the profile HAS but the operator disabled must be refused at
    create, matching the worker's disabled_as_missing semantics (a preload
    must not force a disabled skill)."""
    home = _isolated_home(tmp_path, monkeypatch)
    (home / "profiles" / PROFILE_A / "config.yaml").write_text(
        f"model: test-model\nskills:\n  disabled:\n    - {SKILL_X}\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        try:
            kb.create_task(conn, title="forced disable", assignee=PROFILE_A, skills=[SKILL_X])
            raise AssertionError("create accepted an operator-disabled skill")
        except ValueError as exc:
            assert SKILL_X in str(exc)


def test_create_without_assignee_or_unknown_profile_fails_open(tmp_path, monkeypatch):
    """Unassigned cards skip validation (the dispatcher profile gate owns
    routing); a profile this host cannot resolve is unverifiable, not a
    refusal reason (cross-host boards must still accept the card)."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="no assignee", skills=[SKILL_X])
        t2 = kb.create_task(conn, title="foreign profile", assignee="ghost-profile", skills=[SKILL_X])
    assert t1 and t2


def test_error_message_suggests_near_miss_name(tmp_path, monkeypatch):
    """A typo against a skill the profile DOES have should suggest it:
    'alpha-skil' → 'alpha-skill'."""
    _isolated_home(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        try:
            kb.create_task(conn, title="typo", assignee=PROFILE_A, skills=["alpha-skil"])
            raise AssertionError("create accepted the typo name")
        except ValueError as exc:
            assert "alpha-skil" in str(exc)
            assert SKILL_X in str(exc)  # the near-miss suggestion
