"""A short-lived, single-use pairing code for workstation enrolment.

``working/team/enrolment/PLAN.md`` §1: the owner clicks "+ Add a workstation"
and reads a short code off his own screen; the workstation types it in and
joins. This module is the code itself — issuing it, showing its live state,
letting the owner cancel it, and burning it on redemption. It knows nothing
about what redemption *does* afterwards (manifest, secret, rescan) — that is
a separate module (S2), and this one must never import it.

**One code at a time, on purpose.** PLAN.md §0 calls this "a household, not a
fleet." Issuing a second code immediately kills the first rather than the
store holding two, so there is never a question of *which* code is the one
on the owner's screen right now.

**In memory, and that is not a bug waiting to be fixed.** A pairing code
outlives neither its five-minute TTL nor a process restart. Writing a
credential-adjacent value to appdata to survive neither is cost with no
benefit, and it would be one more file a health check has to explain the
absence of. If a restart happens mid-pairing, the owner clicks the button
again — that is the whole recovery, and it is cheaper than a durable store.

**Never logged, never returned except once.** The code leaves this module in
exactly one shape: the return value of :func:`issue`. Every other read
(:func:`current`) reports state *about* the code — status, expiry, who
claimed it — never the code itself. Nothing in this module calls a logger at
all, which is the simplest way to guarantee none of them ever logs it by
accident.

**Comparison is constant time.** :meth:`PairingStore.redeem` compares with
:func:`hmac.compare_digest`, pads both sides to one fixed length first (its
own documentation: the constant-time guarantee holds exactly when the two
inputs are the same length, and CPython may return early otherwise), and runs
that comparison whether or not there is actually a live code to check against
— see ``personacore.api.keys.ApiKeyStore.verify`` for the reasoning this
borrows. A code the owner is looking at on his screen must not be
brute-forceable while he reads it, and nothing about the timing of a guess
may say whether it was close, wrong, aimed at a code that had already
expired, superseded or used, or simply the wrong length. That last one is
checked too — a candidate longer than the real code must not match on a
correct prefix — but as a second boolean computed unconditionally and
combined with ``&`` rather than a branch, so the length check adds no earlier
exit for the timing to leak through.
"""

from __future__ import annotations

import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# Crockford base32 — no I, L, O, U — so a code read off a screen and typed by
# hand cannot be confused for a different one.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# crockford.com/base32.html, the decode side's own tolerance rule: "the
# encoded value should tolerate ... o and O interpreted as 0 (zero) ... i, I,
# l, L interpreted as 1 (one)". The encoding alphabet above excludes these
# four letters because they are shaped like other characters; this is the
# other half of that — mapping a mistyped look-alike back to the digit it was
# read as, rather than silently dropping it and shortening the candidate.
# `U` gets no entry: it is excluded for an unrelated reason (crockford.com:
# avoiding accidental obscenity), is not shaped like a digit, and has nothing
# to stand in for — a stray `U` is dropped like any other out-of-alphabet
# character, same as a hyphen or a space.
_DECODE_SUBSTITUTIONS = {"O": "0", "I": "1", "L": "1"}

DEFAULT_CODE_BYTES = 5
"""5 random bytes is 40 bits, exactly 8 Crockford characters with no partial
final group — the length on the pairing-screen mockup the owner has already
seen (PLAN.md §1: ``7K4M-2QP9``). Whether this is long enough against the
plaintext-enrolment risk PLAN.md §7 raises is **not decided**
(PLAN.md §8, MINE — needs sign-off). This is a parameter with a documented
default precisely so that answer can change later without touching a route or
this module's callers — do not let a fixed length leak into the API surface
above this module."""

DEFAULT_GROUP_SIZE = 4
"""Hyphenated every 4 characters for reading, matching the approved mockup."""

DEFAULT_TTL_SECONDS = 300
"""Five minutes (this subtask's spec)."""

DEFAULT_MAX_ATTEMPTS = 5
""""The code dies after a small number of [bad redemption attempts] (5)" —
this subtask's spec."""


class PairingState(StrEnum):
    """What the pairing screen renders.

    The first four are this subtask's spec for ``GET .../current``. The last
    two exist because *spent* turned out to be three different things to the
    person watching the dialog, and the screen was rendering all three as
    ``claimed``:

    * a machine redeemed the code and joined — :data:`CLAIMED`;
    * a machine redeemed the code and was refused — :data:`REFUSED`;
    * a machine redeemed the code a moment ago and the answer is not back yet
      — :data:`SETTLING`.

    Collapsing the last two into ``claimed`` produced a dialog that said a
    machine had joined when one had been turned away, with no name to show for
    it. The distinction is read from what the enroller reported, never guessed
    from a timer.
    """

    NONE = "none"
    WAITING = "waiting"
    SETTLING = "settling"
    """Redeemed, outcome unknown. **Never fabricates either answer.** It is
    what the store knows between :meth:`PairingStore.redeem` returning true and
    the enroller saying how it went — ordinarily a split second, and for as
    long as the code's own TTL if the enroller never says at all."""

    CLAIMED = "claimed"
    REFUSED = "refused"
    """A machine redeemed the code and did not join. Carries
    :class:`PairingRefusal`, which says what happened in a form the dialog can
    render without inventing one."""

    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class IssuedPairing:
    """What :meth:`PairingStore.issue` returns.

    ``code`` appears here and nowhere else — not in :class:`PairingSnapshot`,
    not in a log line, not recoverable by any later call.
    """

    code: str
    expires_at: datetime
    expires_in_s: int


@dataclass(frozen=True, slots=True)
class PairingRefusal:
    """Why a redeemed code did not become a machine — for the dialog to render.

    **A code and a name, never a sentence.** The refusal's own wording is
    written for the person standing at the *workstation* and goes back over the
    wire to the Agent; it is free text, it can be long, and it is not this
    module's to carry. What crosses to the admin screen is the short reason
    code the enroller already records in its audit row, plus the name the
    machine gave — and the screen owns the sentence it renders for each.

    That split is what keeps the wording in one place. Two copies of "That
    name is taken" would drift, and the copy on this side would be the one
    nobody reviewed.
    """

    reason: str
    """A short code — lowercase, digits and underscores, no spaces. The same
    vocabulary the enrolment audit row uses, checked through
    :func:`personacore.enrolment.registry.safe_reason` so a sentence cannot be
    passed off as one. ``refused`` is the generic, and the screen must have a
    rendering for it: a refusal with no more specific code is still a refusal
    and still has to close the dialog."""

    machine: str | None
    """What the machine called itself, normalised, or ``None`` if it was turned
    away before it said.

    Held on exactly the same terms as :attr:`PairingSnapshot.claimed_by` — in
    memory, never written to a file, dropped with the code's own TTL. A machine
    that was refused is *not* enrolled, so this name belongs to no registry and
    to nothing that outlives the dialog it is drawn in.
    """


@dataclass(frozen=True, slots=True)
class PairingSnapshot:
    """What :meth:`PairingStore.current` returns — the screen's poll.

    Never the code, under any ``status``.
    """

    status: PairingState
    expires_at: datetime | None
    expires_in_s: int
    claimed_by: str | None
    """The machine that joined. Set only under :data:`PairingState.CLAIMED`,
    so a screen reading it never has to decide what a missing name means."""

    refusal: PairingRefusal | None = None
    """Why it did not join. Set only under :data:`PairingState.REFUSED`.

    Defaulted, so every caller written before this field existed builds the
    same snapshot it always did.
    """


@dataclass
class _ActiveCode:
    """One issued code and what has happened to it. Never leaves this module."""

    raw: str
    """Bare alphabet characters, no hyphens — what :meth:`PairingStore.redeem`
    compares against. The hyphenated, display form lived only in the one
    :class:`IssuedPairing` this code was issued as."""

    expires_at: datetime
    attempts: int = 0
    spent: bool = False
    claimed_by: str | None = None
    refusal: PairingRefusal | None = None
    """Set by :meth:`PairingStore.mark_refused`. Mutually exclusive with
    ``claimed_by`` — one code has one outcome, and whichever lands first is the
    one the dialog shows."""


def _encode(raw: bytes) -> str:
    """Crockford base32, no padding character — every input byte becomes
    whole 5-bit groups when ``len(raw) * 8`` divides evenly by 5, which is
    true for :data:`DEFAULT_CODE_BYTES`."""
    bits = "".join(f"{byte:08b}" for byte in raw)
    bits += "0" * ((-len(bits)) % 5)
    return "".join(_ALPHABET[int(bits[i : i + 5], 2)] for i in range(0, len(bits), 5))


def _group(code: str, size: int) -> str:
    """Hyphenate for reading. ``size <= 0`` means "don't"."""
    if size <= 0:
        return code
    return "-".join(code[i : i + size] for i in range(0, len(code), size))


def _normalise(candidate: str) -> str:
    """What a human typed, reduced to the bare alphabet.

    Upper-cases, then applies Crockford's own decode-side substitutions
    (:data:`_DECODE_SUBSTITUTIONS`) so a mistyped look-alike becomes the digit
    it was read as rather than vanishing — dropping it here would silently
    shorten the candidate, which is a second, unrelated way to fail to match.
    Whatever is left that is still not in the encoding alphabet is dropped —
    hyphens, spaces, a stray newline pasted along with it, and ``U``, which
    has no substitution. Never raises: an unparseable paste just normalises
    to a string that will not match, which :meth:`PairingStore.redeem` already
    handles as an ordinary wrong guess.
    """
    upper = candidate.upper()
    substituted = "".join(_DECODE_SUBSTITUTIONS.get(ch, ch) for ch in upper)
    return "".join(ch for ch in substituted if ch in _ALPHABET)


def _fixed(value: str, length: int) -> str:
    """Pad or truncate to exactly ``length`` characters, for one comparison.

    This alone does **not** reject an overlong candidate — it truncates one
    silently, which is why it is not the whole check. It exists only so that
    ``hmac.compare_digest``'s two arguments are always the same length: its
    own documentation notes CPython may return early when they differ, so
    equalising length first is what makes the constant-time guarantee
    actually apply. :meth:`PairingStore.redeem` checks the *real* length
    separately and combines the two results without a branch — see there.
    """
    return value.ljust(length, "0")[:length]


class PairingStore:
    """The one active pairing code, and its whole lifecycle.

    A single slot, not a table (module docstring: "a household, not a
    fleet"). Every method takes the one lock and holds it only long enough to
    read or mutate that slot — there is no I/O inside it, so contention is not
    a real concern at household scale.

    ``now`` is accepted by every method that needs the current time, the same
    testability seam ``personacore.auth.throttle.SignInThrottle`` uses: real
    callers never pass it, and a test can hand a fixed instant instead of
    sleeping through a five-minute TTL.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        code_bytes: int = DEFAULT_CODE_BYTES,
        group_size: int = DEFAULT_GROUP_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._code_bytes = code_bytes
        self._group_size = group_size
        self._max_attempts = max_attempts
        self._lock = threading.Lock()
        self._active: _ActiveCode | None = None
        # The fixed length every `redeem` comparison is padded to — computed
        # once from `code_bytes` rather than re-derived per call.
        self._raw_length = len(_encode(bytes(code_bytes)))
        self._decoy = "0" * self._raw_length

    def _remaining(self, expires_at: datetime, now: datetime) -> int:
        return max(0, int((expires_at - now).total_seconds()))

    def _is_live(self, active: _ActiveCode, now: datetime) -> bool:
        """Redeemable right now: not spent, not timed out, not attempt-exhausted."""
        return (
            not active.spent
            and now < active.expires_at
            and active.attempts < self._max_attempts
        )

    # -- issuing, showing, cancelling --------------------------------------

    def issue(self, *, now: datetime | None = None) -> IssuedPairing:
        """Mint a code, superseding whatever was live (module docstring)."""
        moment = now or datetime.now(UTC)
        with self._lock:
            raw = _encode(secrets.token_bytes(self._code_bytes))
            expires_at = moment + self._ttl
            self._active = _ActiveCode(raw=raw, expires_at=expires_at)
            return IssuedPairing(
                code=_group(raw, self._group_size),
                expires_at=expires_at,
                expires_in_s=self._remaining(expires_at, moment),
            )

    def current(self, *, now: datetime | None = None) -> PairingSnapshot:
        """The screen's poll.

        Expiry is computed here, live, on every call — not by a timer that
        flips a stored flag — which is this subtask's spec, spelled out
        because the alternative looks like an obvious optimisation to a later
        reader and is exactly the thing not to do.
        """
        moment = now or datetime.now(UTC)
        with self._lock:
            active = self._active
            if active is None:
                return PairingSnapshot(PairingState.NONE, None, 0, None)
            if active.spent:
                # A spent code is dropped once its own TTL has run out, and the
                # display name goes with it — under either outcome. A machine's
                # name is held in core memory here: nothing writes it to disk,
                # and this is what stops it outliving the code it belongs to and
                # being readable from the pairing screen long after the plugin
                # that machine belongs to has been uninstalled. Five minutes is
                # the dialog's own lifetime, not a new number.
                if moment >= active.expires_at:
                    self._active = None
                    return PairingSnapshot(PairingState.NONE, None, 0, None)
                if active.claimed_by is not None:
                    return PairingSnapshot(
                        PairingState.CLAIMED, active.expires_at, 0, active.claimed_by
                    )
                if active.refusal is not None:
                    return PairingSnapshot(
                        PairingState.REFUSED, active.expires_at, 0, None, active.refusal
                    )
                # Redeemed, and the enroller has not said how it went. **Not
                # reported as claimed.** It was, and a dialog rendering the
                # claimed state printed a machine name that was `None` — which
                # is also exactly what a refusal looked like, so the screen
                # could not tell a machine joining from a machine turned away.
                return PairingSnapshot(PairingState.SETTLING, active.expires_at, 0, None)
            if self._is_live(active, moment):
                return PairingSnapshot(
                    PairingState.WAITING,
                    active.expires_at,
                    self._remaining(active.expires_at, moment),
                    None,
                )
            return PairingSnapshot(PairingState.EXPIRED, active.expires_at, 0, None)

    def cancel(self, *, now: datetime | None = None) -> bool:
        """Kill whatever is live. The owner closing the dialog kills the code.

        Returns whether anything was actually live to kill — not whether a
        code merely existed — so the caller's audit record can say ``existed``
        the same way ``api_keys.revoke`` does, rather than claiming a cancel
        happened when the code was already dead.
        """
        moment = now or datetime.now(UTC)
        with self._lock:
            active = self._active
            was_live = active is not None and self._is_live(active, moment)
            self._active = None
            return was_live

    def mark_claimed(self, display_name: str) -> None:
        """Record who redeemed the live code, once the enroller has a name.

        A no-op if nothing is spent right now: a code cancelled or superseded
        between redemption and this call has nothing left to name, and there
        is nothing useful to raise about that here — the enroller already has
        its own success or failure to report.

        **This is the one place a machine's name enters core state.** It is
        held in memory, is never written to a file, and :meth:`current` drops
        it once the code's own TTL has run out, so it cannot be read back off
        the pairing screen after the machine it names has been removed.
        """
        with self._lock:
            active = self._active
            if active is not None and active.spent and active.refusal is None:
                active.claimed_by = display_name

    def mark_refused(self, display_name: str | None, reason: str) -> None:
        """Record that the machine which redeemed the live code did not join.

        The other half of :meth:`mark_claimed`, and the reason the dialog can
        close honestly. Without it a redeemed-then-refused code is
        indistinguishable from a code redeemed a split second ago, because both
        are spent with no name — so the screen either waits forever or claims a
        machine joined when one was turned away.

        Args:
            display_name: what the machine called itself, or ``None`` if it was
                refused before it said. Normalised by the enroller.
            reason: a short code from the enrolment vocabulary. Anything that is
                not a code becomes ``refused`` —
                :func:`personacore.enrolment.registry.safe_reason` is the one
                place that rule lives, and it is applied rather than trusted,
                because a caller passing the refusal *sentence* is exactly how a
                machine's name and an address would reach this screen.

        A no-op if nothing is spent right now, or if the code has already been
        claimed: one code has one outcome, and a success that has already landed
        is not overwritten by a straggler.

        **This tells an unauthenticated caller nothing.** The refusal is written
        by the core, on the path that already answered the Agent with the full
        sentence, and is read only on the admin surface, which is guarded. The
        direction that needed guarding is this one, and nothing here travels
        back out through the enrolment route.
        """
        from personacore.enrolment.registry import safe_reason  # noqa: PLC0415 - see below

        # Imported inside the call rather than at module scope: this module is
        # deliberately free of every other part of enrolment (see the module
        # docstring — it must never import the half that acts on a redemption),
        # and the one thing it borrows is a shared rule about what a reason code
        # may look like, not a collaborator.
        with self._lock:
            active = self._active
            if active is not None and active.spent and active.claimed_by is None:
                active.refusal = PairingRefusal(
                    reason=safe_reason(reason), machine=display_name
                )

    # -- redemption ---------------------------------------------------------

    def redeem(self, code: str, *, now: datetime | None = None) -> bool:
        """Burn the live code if ``code`` matches it.

        See the boundary docstring on the module-level :func:`redeem` — this
        is its implementation, and the contract is stated there because that
        is the name another module imports.

        **The length check does not branch.** ``_fixed`` truncates, so on its
        own it would let an overlong candidate — the right code with junk
        appended, or a paste one character too long — match on its correct
        prefix. The fix is not ``if len(candidate) != self._raw_length: return
        False`` before the comparison: that would resolve a wrong-length
        guess faster than a right-length wrong guess, and the timing itself
        would then say "your length was wrong" — exactly the oracle the fixed-
        length padding exists to close. Instead both ``content_match`` and
        ``length_match`` are computed unconditionally, in either order, and
        combined with ``&`` (never ``and``, which short-circuits) so neither
        one's result decides whether the other is evaluated.
        """
        moment = now or datetime.now(UTC)
        candidate = _normalise(code)
        with self._lock:
            active = self._active
            live = active is not None and self._is_live(active, moment)
            target = active.raw if live and active is not None else self._decoy
            content_match = hmac.compare_digest(
                _fixed(candidate, self._raw_length), _fixed(target, self._raw_length)
            )
            length_match = len(candidate) == self._raw_length
            matched = content_match & length_match
            if live and matched and active is not None:
                active.spent = True
                return True
            if active is not None and not active.spent:
                active.attempts += 1
            return False


# ---------------------------------------------------------------------------
# The one store this process has
# ---------------------------------------------------------------------------

_default_store = PairingStore()
"""The process-wide singleton. There is exactly one of these per running
core (module docstring: "a household, not a fleet"), which is why every
function below takes no store argument — the admin API that issues and shows
the code, and the enrolment route that redeems it, must agree on the same
one without either side having to be handed it."""


def issue() -> IssuedPairing:
    """Mint a code against the one store this process has."""
    return _default_store.issue()


def current() -> PairingSnapshot:
    """The screen's poll, against the one store this process has."""
    return _default_store.current()


def cancel() -> bool:
    """Kill the live code. Returns whether anything was actually live to kill."""
    return _default_store.cancel()


def mark_claimed(display_name: str) -> None:
    """Record who redeemed the live code, once the enroller has a name."""
    _default_store.mark_claimed(display_name)


def mark_refused(display_name: str | None, reason: str) -> None:
    """Record that the machine which redeemed the live code did not join."""
    _default_store.mark_refused(display_name, reason)


def redeem(code: str) -> bool:
    """Burn a pairing code. True if it was the live code and is now spent.

    Handles expiry, the failure counter and single-use burning internally. The
    caller gets one boolean and learns nothing else — not whether a code was
    wrong, expired, superseded or already used, because the enrolment route
    must not distinguish them (S2). Constant time.
    """
    return _default_store.redeem(code)


__all__ = [
    "DEFAULT_CODE_BYTES",
    "DEFAULT_GROUP_SIZE",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TTL_SECONDS",
    "IssuedPairing",
    "PairingRefusal",
    "PairingSnapshot",
    "PairingState",
    "PairingStore",
    "cancel",
    "current",
    "issue",
    "mark_claimed",
    "mark_refused",
    "redeem",
]
