"""A pre-start managed worker failure settles terminal failed with the worker's real reason.

``kanban_skills_unavailable`` and friends raise before the turn starts: the worker ran no
model, no tool and published no delta, so nothing may have been half-executed. The honest
settlement is an ordinary failed turn whose reply names the reason — never ``unknown``, the
outcome reserved for losses that may have run halfway and which parks every follower behind
the turn. Error frames WITHOUT the pre-start marker keep the unknown path byte-for-byte
(follower pause included: it is triggered by the ``ManagedExecutionUnknown`` the drain sees).
"""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.session_contract import Principal, SessionRef

_PRE_START_REASON = 'kanban_skills_unavailable: billing-ops'

# Sends hello, waits for the bootstrap frame (so the reservation committed), then reports a
# pre-start failure with the worker's reason — the frame shape managed_worker.main sends when
# the interpreter dies before any delta/tool/result frame went out.
PRE_START_CHILD = f"""import json, os, sys, psutil
me = psutil.Process()
out = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
out.write((json.dumps({{'type': 'hello', 'pid': me.pid, 'birth': me.create_time(),
                       'ancestors': [p.pid for p in me.parents()[:3]]}}) + '\\n').encode())
for line in sys.stdin.buffer:
    out.write((json.dumps({{'type': 'error', 'reason': 'managed_worker_failed',
        'pre_start_reason': '{_PRE_START_REASON}'}}) + '\\n').encode())
    break
"""

# Today's exact error frame: no marker, so the owner must keep the unknown settlement and the
# follower pause (the unknown/lineage regressions pin the rest of this path).
LEGACY_CHILD = """import json, os, sys, psutil
me = psutil.Process()
out = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
out.write((json.dumps({'type': 'hello', 'pid': me.pid, 'birth': me.create_time(),
                       'ancestors': [p.pid for p in me.parents()[:3]]}) + '\\n').encode())
for line in sys.stdin.buffer:
    out.write(b'{"type": "error", "reason": "managed_worker_failed"}\\n')
    break
"""


def _started_worker(tmp_path, monkeypatch, child_source):
    """One submitted turn whose reserved worker dies on ``child_source``'s first frame out.

    Returns ``(run, db, authority, published, policy)``; ``run(scenario)`` submits, claims
    and executes ``scenario(authority, ref, row, policy)`` under one open db, like the drain
    would claim it.
    """
    from gateway import session_managed_worker as managed
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import Submission
    from gateway.session_policy import build_policy
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s', source='test')
    runner = SimpleNamespace(_draining=False, _cached_agent_for=lambda route: None,
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='i', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='i'))
    live = authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='human', chat_id='c'), 'route')
    published = []
    original = live.event_stream.publish
    monkeypatch.setattr(live.event_stream, 'publish', lambda sid, payload, **kw: (
        published.append((kw.get('event_type', 'message.complete'), payload)), original(sid, payload, **kw))[1])
    actor = Principal('human', 'owned', frozenset({'session:submit', 'session:control'}), 'cli')
    ref = SessionRef('owned', 's')
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)

    class Child(subprocess.Popen):
        def __init__(self, args, **kw):
            super().__init__([sys.executable, '-c', child_source], **kw)
    monkeypatch.setattr(managed.subprocess, 'Popen', Child)
    policy = build_policy({'cwd': str(tmp_path), 'model': 'm'}, {}, profile_terminal=False)

    async def _run(scenario):
        with db:
            await authority.submit(actor, Submission('turn', ref, {'text': 'go'}, 'queue'))
            row = claim_session_input(db, epoch=authority.epoch, session_id='s')
            # The drain stamps this between claim and execution (session_authority.py:1110);
            # without it the frame loop's generation guard fires before the first frame read.
            authority.executing[ref.session_id] = row['generation']
            async with asyncio.timeout(30):
                return await scenario(authority, ref, row, policy)

    def run(scenario):
        return asyncio.run(_run(scenario))
    return run, db, authority, published, policy


@pytest.mark.platforms("linux")
def test_prestart_error_frame_settles_failed_with_the_real_reason(tmp_path, monkeypatch):
    """Pre-start marker → no unknown outcome, worker reservation closed, reply names the reason.

    Acceptance values: the marker text and the failed settlement it must
    produce — not recomputed from the implementation.
    """
    from gateway import session_managed_worker as managed
    from gateway.session_results import prepare_result
    from gateway.session_settlement_recovery import completion_payload

    async def scenario(authority, ref, row, policy):
        return row, await managed.execute_managed(authority, ref, row, policy)
    run, db, authority, published, _policy = _started_worker(tmp_path, monkeypatch, PRE_START_CHILD)
    row, response = run(scenario)

    # The turn settles as an ordinary failed turn: execute_managed returned instead of raising
    # ManagedExecutionUnknown (the drain's sole follower-pause trigger) and nothing published
    # an unknown outcome on the way.
    assert _PRE_START_REASON in response
    assert not [p for _, p in published if p.get('outcome') == 'unknown'], published
    with db._read_ctx() as conn:
        assert conn.execute('SELECT status FROM worker_executions').fetchone()[0] == 'terminal'
        # The admission itself is left to the drain's normal settlement (the interrupted-turn
        # precedent: capture the result, return, let commit_and_publish write the terminal row).
        assert tuple(conn.execute('SELECT status, outcome FROM session_admissions').fetchone()) == ('started', None)

    # The drain's settlement contract on the captured result: the failed flag commits
    # outcome 'failed' and the completion frame's reply carries the real reason.
    captured = authority.pending_results[row['admission_id']]
    assert captured['result']['failed'] is True
    assert _PRE_START_REASON in captured['result']['final_response']
    outcome, _stored = prepare_result(row, response, 'completed', captured)
    assert outcome == 'failed'
    complete = completion_payload(row, {'outcome': 'failed'}, response, captured)
    assert complete['status'] == 'error'
    assert _PRE_START_REASON in complete['text']


@pytest.mark.platforms("linux")
def test_unmarked_error_frame_keeps_the_unknown_settlement(tmp_path, monkeypatch):
    """The byte-for-byte legacy error frame keeps unknown (and the follower pause it drives).

    Characterization test of the frozen path: unchanged behaviour, green before and
    after the change (independent requirement: other error frames keep the
    current unknown settlement).
    """
    from gateway import session_managed_worker as managed

    async def scenario(authority, ref, row, policy):
        return await managed.execute_managed(authority, ref, row, policy)
    run, db, _authority, published, _policy = _started_worker(tmp_path, monkeypatch, LEGACY_CHILD)
    with pytest.raises(managed.ManagedExecutionUnknown):
        run(scenario)
    with db._read_ctx() as conn:
        assert conn.execute('SELECT status FROM session_admissions').fetchone()[0] == 'unknown'
    unknown = [p for _, p in published if p.get('outcome') == 'unknown']
    assert unknown and unknown[0]['status'] == 'error'


# Worker-interpreter driver: stubs the bootstrap read and the turn execution so main() dies
# in the turn phase (after the agent is constructed and ready was sent) either BEFORE any
# progress frame (pre-start) or AFTER one delta was sent (mid-turn), then asserts which error
# frame reached the wire. The raise text is the card's example reason.
MAIN_CHILD = """import os, sys
from agent import managed_worker as mw

def _fake_read(stream):
    return {}

def _execute(frame, channel):
    channel.send('ready', pid=os.getpid())
    if 'progress' in sys.argv:
        channel.send('delta', text='partial answer')
    raise ValueError('kanban_skills_unavailable: billing-ops')

mw.read_frame = _fake_read
mw.validate_bootstrap = lambda frame: frame
mw.execute = _execute
sys.exit(mw.main())
"""


def _worker_error_frame(*flags):
    import json
    import subprocess
    proc = subprocess.run([sys.executable, '-c', MAIN_CHILD, *flags],
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 1, proc.stderr[-800:]
    frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return [f for f in frames if f.get('type') == 'error']


@pytest.mark.platforms("linux")
def test_worker_marks_the_error_frame_pre_start_only_before_any_progress_frame():
    """main() names the real reason on the error frame only while nothing proved execution.

    Acceptance shape on the worker side: the turn-phase failure carries the
    missing-list text; after a delta was sent the frame stays the byte-for-byte legacy shape.
    """
    frames = _worker_error_frame()
    assert frames == [{'type': 'error', 'reason': 'managed_worker_failed',
                       'pre_start_reason': _PRE_START_REASON}], frames
    frames = _worker_error_frame('progress')
    assert frames == [{'type': 'error', 'reason': 'managed_worker_failed'}], frames


def test_skills_unavailable_names_the_missing_skills(monkeypatch):
    """The skills wall names what is missing, so the pre-start reason the owner replies with
    is actionable (verbatim acceptance string).
    """
    from types import SimpleNamespace

    from gateway import session_kanban
    from agent import skill_commands

    monkeypatch.setattr(skill_commands, 'build_preloaded_skills_prompt',
                        lambda names, task_id=None: ('', [], list(names)))
    agent = SimpleNamespace(session_id='s')
    context = {'skills': ['billing-ops'], 'context': '', 'goal_mode': False}
    with pytest.raises(ValueError) as raised:
        session_kanban._run_task_turns(agent, {'text': 'go'}, [], context)
    assert str(raised.value) == _PRE_START_REASON
