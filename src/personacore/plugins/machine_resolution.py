"""Turning "the loft one" into a machine — reshape plan R3.

Owner decision (``working/team/enrolment-v2/PLAN.md`` section 0.3): the
machine a tool call runs on is an *argument* — ``workstation.shell_run`` with
a ``machine`` parameter — never a tool name of its own. Four rules, his, in
his order:

1. Not specified: never guess, never pick a default. Present the list and ask.
2. Match on **name or IP**, either. Partial name matching is expected.
3. A partial match that hits more than one machine is ambiguous: present the
   list again. No silent nearest-match, ever.
4. An exact name match wins over a partial one. The only precedence rule.

This module is a pure matcher over :class:`MachineCandidate` — a small, local
type carrying nothing that requires an import of ``personacore.enrolment``.
Building the real candidate list — joining an
:class:`~personacore.plugins.supervisor.EndpointSetSupervisor`'s machines with
the names :class:`~personacore.enrolment.registry.MachineRegistry` holds — is
a composition-root job, following the same shape
``MachineRegistry.connection_state`` and
``EndpointSetSupervisor.on_machine_acted`` already use to cross this exact
boundary: a callable handed in, never an import added. ``plugins/`` stays
ignorant of what a machine is called by anybody outside it; this module only
ever sees the candidates it is given.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

MACHINE_ARGUMENT = "machine"
"""The tool argument naming which machine to run on (PLAN.md 0.3: "one flat
catalogue — ``workstation.shell_run`` with a machine parameter"). Consumed at
the plugin-host boundary and never forwarded to a machine's own remote tool —
one machine's own MCP server has no use for a word naming which machine it
is."""


@dataclass(frozen=True, slots=True)
class MachineCandidate:
    """One machine, as the resolver sees it: nothing it cannot compare what
    was typed against, and nothing it cannot show the owner back.

    Deliberately not :class:`personacore.enrolment.registry.Machine` — that
    type belongs to enrolment, and a resolver built to take it would be the
    layering inversion this module exists to avoid. A caller adapts whatever
    it holds into this instead.
    """

    name: str
    addresses: tuple[str, ...]
    online: bool | None
    """``True``/``False`` from whoever tracks connections; ``None`` when that
    is not known. Never used to decide *which* machine was meant — see
    :func:`resolve_machine` — only carried through for a clickable list to
    show, and for a resolved machine's own offline message."""


@dataclass(frozen=True, slots=True)
class MachineResolved:
    """Exactly one machine was meant.

    ``address`` is one of the machine's own addresses — any of them names the
    same machine to
    :meth:`~personacore.plugins.supervisor.EndpointSetSupervisor.call` — taken
    as the candidate's first, which is also its stable identity row
    (``EndpointHealth.url``).
    """

    candidate: MachineCandidate
    address: str


@dataclass(frozen=True, slots=True)
class MachineNeedsAsking:
    """More than one candidate remains, because nothing was said or because
    what was said matched more than one machine, and the resolver must ask
    rather than guess (owner decision: no silent nearest-match, ever).

    ``query`` is ``None`` for "nothing specified"; the raw text for a partial
    match that hit more than one machine. Carries every
    :class:`MachineCandidate` a future clickable list needs — name, addresses,
    online state — so a later disambiguation UI can render one without asking
    this module anything further.
    """

    query: str | None
    candidates: tuple[MachineCandidate, ...]


@dataclass(frozen=True, slots=True)
class MachineNotFound:
    """What was typed matched nothing. Still not a guess: every candidate
    rides along so the same clickable list can be offered instead."""

    query: str
    candidates: tuple[MachineCandidate, ...]


MachineResolution = MachineResolved | MachineNeedsAsking | MachineNotFound


def resolve_machine(
    raw: str | None, candidates: Sequence[MachineCandidate]
) -> MachineResolution:
    """The matching rule, in one sentence: compare what was typed,
    case-insensitively and with whitespace collapsed, against each machine's
    name — as a substring that starts the name or one of its
    hyphen/underscore/space/dot-separated parts (:func:`_name_matches_at_boundary`)
    — or as a substring anywhere in any of its addresses, and treat anything
    other than exactly one match as a question rather than a guess — with one
    exception, that a match on a machine's full name exactly wins outright
    even if the same text also partially matches others.

    ``candidates`` is never filtered by ``online`` here.
    :meth:`~personacore.plugins.supervisor.EndpointSetSupervisor.call`'s own
    docstring gives the reason one layer down — being offline must never
    promote a machine to the one that was *configured* — and the same
    reasoning applies here, one layer up, to which machine was *named*: three
    machines with two asleep is still three machines to choose among.
    """
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        if len(candidates) == 1:
            return _resolved(candidates[0])
        return MachineNeedsAsking(query=None, candidates=tuple(candidates))

    folded_query = _fold(text)
    matches = [c for c in candidates if _matches(text, folded_query, c)]
    exact_name = [c for c in matches if _fold(c.name) == folded_query]
    if len(exact_name) == 1:
        return _resolved(exact_name[0])
    if not matches:
        return MachineNotFound(query=text, candidates=tuple(candidates))
    if len(matches) == 1:
        return _resolved(matches[0])
    return MachineNeedsAsking(query=text, candidates=tuple(matches))


def listing(candidates: Sequence[MachineCandidate]) -> str:
    """Every candidate, by name, online or not — the phrase every refusal
    sentence built from a :class:`MachineResolution` shares, so "loft-pc
    (online), kids-pc (offline)" reads the same everywhere it appears."""
    return ", ".join(
        f"{c.name} ({'online' if c.online else 'offline'})" for c in candidates
    )


_NAME_SEPARATORS = ("-", "_", " ", ".")
"""Where one "word" of a machine's name ends and the next begins, for
:func:`_name_matches_at_boundary`. Checked against
``enrolment.workstation._DISPLAY_NAME_RE`` (what a machine may actually be
called: letters, digits, and these four separators, nothing else) rather than
assumed — the review's own suggested fix named only hyphen, underscore and
space, and left out the dot a real hostname is equally likely to carry
(``workshop.pc``). Restated rather than imported: that pattern is private to
``enrolment``, and this module has no business depending on it. Getting this
set wrong in either direction has an asymmetric cost — too few separators
only makes the resolver ask when it could have matched (safe, just
unhelpful); too many would be the silent-wrong-machine risk this function
exists to close. So the set is exactly what the naming rule allows, not a
guess at what "looks like" a separator."""


def _name_matches_at_boundary(folded_query: str, folded_name: str) -> bool:
    """Whether ``folded_query`` lines up with where a human would say a
    machine's name starts — the whole name, or the start of one of its
    hyphen/underscore/space/dot-separated parts — rather than merely occurring
    somewhere inside a word.

    Without this, a plain substring test lets "new" match inside
    "renewed-pc" — the middle of a single word — and if that is the only
    machine it matches, the resolver would hand back exactly one candidate
    and run on it without asking. That is the silent wrong-machine the owner
    forbade (PLAN.md 0.3: "No silent nearest-match"), just reached through
    the matcher instead of through ranking. A query has to start a word of
    the name, not merely appear inside one.
    """
    if not folded_query:
        return False
    index = folded_name.find(folded_query)
    while index != -1:
        if index == 0 or folded_name[index - 1] in _NAME_SEPARATORS:
            return True
        index = folded_name.find(folded_query, index + 1)
    return False


def _matches(text: str, folded_query: str, candidate: MachineCandidate) -> bool:
    if _name_matches_at_boundary(folded_query, _fold(candidate.name)):
        return True
    # Addresses are untouched by the boundary rule above: an IP or URL has no
    # notion of "words" the way a machine name does, and PLAN.md 0.3's
    # partial-IP example ("2001:db8::30" matching inside a full bracketed
    # IPv6 URL) depends on a match landing mid-string. The risk this function
    # exists to close was raised, and is fixed, for names only.
    return any(text in address for address in candidate.addresses)


def _resolved(candidate: MachineCandidate) -> MachineResolved:
    address = candidate.addresses[0] if candidate.addresses else ""
    return MachineResolved(candidate=candidate, address=address)


def _fold(text: str) -> str:
    """Case-insensitive, whitespace-collapsed — the same comparison
    ``enrolment.registry``'s own ``_fold`` uses for a machine's name, restated
    rather than imported: that module's helper is private, and importing it
    would be the one import this module exists to not have."""
    return " ".join(text.split()).casefold()


__all__ = [
    "MACHINE_ARGUMENT",
    "MachineCandidate",
    "MachineNeedsAsking",
    "MachineNotFound",
    "MachineResolution",
    "MachineResolved",
    "listing",
    "resolve_machine",
]
