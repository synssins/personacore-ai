"""Asking an LLM host what it serves, and the words for every way that can
fail (spec section 4).

Split out of the Models screen because it is the part with a network call in
it: every branch below ends in a sentence an operator can act on, and the
screen only has to choose which one to show.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from personacore.admin.protocols import (
    LLMHealthSource,
)
from personacore.config.settings import LLMRole

# ---------------------------------------------------------------------------
# Model discovery — asking a host what it actually serves
# ---------------------------------------------------------------------------
#
# Spec §9 wants this screen click-first, and the model name is the one field on
# it that a person cannot guess: it has to be spelled exactly as the host
# spells it. Every OpenAI-compatible host — Ollama, llama.cpp, vLLM, LM Studio
# — answers `GET {base_url}/models` with the names it serves, so the box can
# offer them instead of demanding them.
#
# Three decisions worth keeping:
#
# * **The host asked is the one in the box**, not the saved one. Someone who
#   has just typed a new address wants the new host's models; asking the saved
#   host would answer a question nobody asked.
# * **The configured model is always an option and always the selected one**,
#   even when the host does not list it. A host that omits a model it can
#   really serve (a lazily-loaded model, a proxy with a narrow catalogue) must
#   never silently blank a working configuration into something else. It is
#   marked as not advertised rather than dropped.
# * **Free text stays reachable.** A host that cannot be asked is not an error
#   — plenty of them do not implement `/models` at all, and plenty want a key
#   before they will. The field falls back to the text box it has always been
#   and the reason is printed under it.

MODEL_FETCH_NO_ADDRESS = "Put an address in the box first."

MODEL_FETCH_UNREACHABLE = (
    "Nothing answered at that address. Check the address and that the host is "
    "switched on — or type the model name in yourself."
)

MODEL_FETCH_NO_LISTING = (
    "That host does not offer a list of the models it serves — plenty don't. "
    "Type the model name in yourself, spelled the way the host spells it."
)

MODEL_FETCH_NEEDS_KEY = (
    "That host will not list its models without an API key, and this role's key "
    "is missing or was refused. Set the key, or type the model name in yourself."
)

MODEL_FETCH_KEY_UNREADABLE = (
    "This role's API key could not be read, so the host was never asked. Check "
    "the secret named in the settings, or type the model name in yourself."
)

MODEL_FETCH_NOT_A_LISTING = (
    "That host answered, but not with a list of models in the shape an "
    "OpenAI-compatible host uses. Type the model name in yourself."
)

MODEL_FETCH_EMPTY = (
    "That host answered with an empty list — no models on offer right now. "
    "Type one in yourself; it may still serve it."
)

MODEL_FETCH_HOST_ERROR = (
    "That host answered with a problem of its own instead of a list. Check the host's "
    "own log — or type the model name in yourself."
)

MODEL_FETCH_UNAVAILABLE = (
    "That host could not be asked for its models just now. Try again in a moment — or "
    "type the model name in yourself."
)

MODEL_NOT_ADVERTISED = "not currently advertised by this host"
"""What an option says when the host did not list the model that is already
set. It stays selectable, and selected: the operator's working configuration is
not this screen's to second-guess."""


def model_choices(listed: Sequence[str], current: str) -> list[dict[str, Any]]:
    """The dropdown's options — what the host reported, plus what is set now.

    ``current`` is always present and always the selected option, at the top of
    the list when the host did not name it, labelled with
    :data:`MODEL_NOT_ADVERTISED`. Duplicates and blanks in the host's own list
    are dropped, because some proxies repeat a model under two names.
    """
    names: list[str] = []
    for item in listed:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)

    choices = [
        {"id": name, "label": name, "listed": True, "selected": name == current}
        for name in names
    ]
    if current and current not in names:
        choices.insert(
            0,
            {
                "id": current,
                "label": f"{current} — {MODEL_NOT_ADVERTISED}",
                "listed": False,
                "selected": True,
            },
        )
    return choices


def model_fetch_summary(choices: Sequence[Mapping[str, Any]]) -> str:
    """The sentence printed under a dropdown that was successfully filled."""
    listed = sum(1 for choice in choices if choice["listed"])
    sentence = f"That host lists {listed} model{'' if listed == 1 else 's'}."
    if any(not choice["listed"] for choice in choices):
        sentence += (
            " The model this role is set to is not among them — kept at the top "
            "so it stays selected."
        )
    return sentence


def _role_health_source(llm: LLMHealthSource, role: LLMRole) -> LLMHealthSource:
    """The probe for one role, or the whole source if it predates roles.

    Six lines rather than a shared helper: this is the whole of what "which
    client answers for this role" means on a screen, and a source that predates
    ADR-0011's split is still a legitimate one.
    """
    views = getattr(llm, "role_views", None)
    if views is None:
        return llm
    for view in views():
        if view.role == role.value:
            return view
    return llm
