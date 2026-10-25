"""Shared resolver: do a card's declared ``skills`` resolve for its ASSIGNEE?

A card whose ``skills`` name a skill the assignee profile cannot load used to
crash its worker at startup (t_9691bf56: eight consecutive crash-loops on a
card pinned to a profile without ``sdlc-review``). All three board entries —
``create_task``, ``assign_task`` and the dispatcher spawn gate — ask THIS
question so their verdicts can never diverge.

Resolution semantics mirror the worker's own load path
(``agent.skill_commands.build_preloaded_skills_prompt`` →
``_load_skill_payload``): the identifier is looked up through ``skill_view``
under the assignee profile's HERMES_HOME (profile ``skills/`` dir plus the
global/external dirs that profile sees), and an operator-disabled skill counts
as missing (``disabled_as_missing`` semantics).

The resolver reports *verdicts*; callers decide enforcement: create/assign
raise, the dispatcher blocks. When the check cannot run here — the assignee's
profile dir is unreadable from this host (cross-host boards), the name is not
a valid profile id, or the safe-worker policy short-circuits loading — the
resolver returns ``None`` and the caller must fail open: the dispatcher's
existing profile gate still buckets a truly-missing profile as
nonspawnable, and the worker-side raise stays as the last line of defense.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class SkillResolution:
    """Outcome of a VERIFIABLE check (``None`` from the resolver means
    not verifiable here). ``missing`` preserves the caller's order."""

    missing: tuple[str, ...]
    available: tuple[str, ...]
    """Skill names the profile CAN resolve today (its own + global view),
    used only to suggest near-miss spellings; may be empty on scan failure."""

    @property
    def ok(self) -> bool:
        return not self.missing


def resolve_profile_skills(
    profile: Optional[str], skills: Optional[Iterable[str]],
) -> Optional[SkillResolution]:
    """Check *skills* against what *profile* can load. Returns ``None`` when
    unverifiable from this host (see module docstring); otherwise a
    :class:`SkillResolution` whose ``missing`` lists unresolvable and
    operator-disabled names."""
    names = [str(s).strip() for s in (skills or []) if str(s or "").strip()]
    profile = (profile or "").strip()
    if not profile or not names:
        # Nothing to check: an unassigned card or an empty skills list is the
        # caller's business, not a resolvability question.
        return SkillResolution(missing=(), available=())
    try:
        from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
        home = resolve_profile_env(normalize_profile_name(profile))
    except Exception:
        # Unknown/invalid profile or a profile dir this host cannot resolve
        # (cross-host board): fail open, the dispatcher's profile_exists gate
        # and the worker's own load path stay authoritative.
        return None
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        from agent.safe_worker_policy import safe_worker_enabled
        if safe_worker_enabled():
            # The policy short-circuits skill loading process-wide; asking
            # "would this resolve" has no meaning under it.
            return None
        from agent.skill_commands import _disabled_skill_names, _load_skill_payload
        disabled = _disabled_skill_names()
        missing: list[str] = []
        for name in names:
            loaded = _load_skill_payload(name)
            if loaded is None:
                missing.append(name)
                continue
            # Mirror _load_skill_blocks' disabled match exactly: the canonical
            # LOADED name, the raw identifier, or a plugin "ns:skill" suffix.
            canonical = str(loaded[2] or "")
            if (canonical in disabled or name in disabled
                    or (":" in canonical and canonical.split(":", 1)[1] in disabled)):
                missing.append(name)
        available = _available_skill_names() if missing else ()
        return SkillResolution(missing=tuple(missing), available=available)
    finally:
        reset_hermes_home_override(token)


def _available_skill_names() -> tuple[str, ...]:
    """Scannable skill names under the (already overridden) home, for
    near-miss suggestions. Scan failure yields an empty view."""
    try:
        from agent.skill_commands import get_skill_commands
        return tuple(
            str(key).lstrip("/") for key in get_skill_commands()
        )
    except Exception:
        return ()


def format_unresolvable_error(assignee: str, resolution: SkillResolution) -> str:
    """Refusal message for create/assign: which names, for which profile,
    what to do about it (drop the field or fix the spelling)."""
    quoted = ", ".join(repr(n) for n in resolution.missing)
    noun = "is not resolvable" if len(resolution.missing) == 1 else "are not resolvable"
    msg = (
        f"skill(s) {quoted} {noun} for assignee profile {assignee!r} "
        f"(unknown to that profile's skills dirs, or operator-disabled). "
        "Leave `skills` empty to let the worker start with no preloaded "
        "skills, or install the skill into that profile"
    )
    suggestions: list[str] = []
    for name in resolution.missing:
        near = difflib.get_close_matches(name, resolution.available, n=3, cutoff=0.6)
        if near:
            suggestions.append(f"{name!r} → " + ", ".join(repr(n) for n in near))
    if suggestions:
        msg += "; similar skills for this profile: " + "; ".join(suggestions)
    else:
        msg += ". Check the profile's skills with `hermes -p " + assignee + " skills list`"
    return msg
