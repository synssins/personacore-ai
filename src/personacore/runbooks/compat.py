"""Whether an already-valid runbook can run against *this core*, right now —
``working/contracts/runbook.md`` §1.8 and §1.10.

Separate from :mod:`personacore.runbooks.validate` on purpose: a runbook can
be perfectly well-formed and still unusable because a plugin it needs is not
installed, is switched off, is too old, or has its own runbooks switch off.
Those are facts about *this household's* plugins, not about the file, so a
runbook uploaded for plugin ``vesmark`` before ``vesmark`` is even installed
still stores and validates — it is simply greyed until the plugin shows up
(contract §6: "uploads for it still validate and store").

:class:`PluginFacts` is a narrow :class:`typing.Protocol` and this module
imports no plugin host of any kind. Whoever builds one — in production,
``server.py``, over the real ``PluginHost`` and its manifests — decides what
"installed", "enabled" and "supports runbooks" mean; this module only knows
how to turn those three facts, plus a runbook's own ``requires:``, into a
verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from personacore.runbooks.schema import Runbook


@dataclass(frozen=True)
class Verdict:
    """Whether a runbook may be offered to run, and why not if it may not.

    ``reasons`` is empty exactly when ``ok`` is true — never a partial
    explanation for an outcome that was fine anyway.
    """

    ok: bool
    reasons: list[str] = field(default_factory=list)


class PluginFacts(Protocol):
    """What :func:`check` needs to know about one named plugin.

    Every method takes the plugin's own name and answers about *that*
    plugin only — there is no "list everything installed" here, because
    :func:`check` never needs one and a narrower protocol is a narrower
    thing to fake in a test.
    """

    def installed(self, name: str) -> str | None:
        """The installed version, or ``None`` if the plugin is not installed."""
        ...

    def enabled(self, name: str) -> bool:
        """Whether the plugin itself is switched on and running."""
        ...

    def declares_tool(self, name: str, tool: str) -> bool:
        """Whether the plugin's manifest declares a tool of this name."""
        ...

    def supports_runbooks(self, name: str) -> bool:
        """The manifest's ``[runbooks] supported`` (contract §1.10)."""
        ...


def check(
    runbook: Runbook,
    facts: PluginFacts,
    *,
    plugin_runbooks_on: Callable[[str], bool],
) -> Verdict:
    """Contract §1.8: a runbook greys itself out, it does not merely fail.

    Checked in the order a person would ask the questions: is the plugin
    there at all, is it switched on, does it even know what a runbook is,
    is *this* runbook's use of it switched on, and only then, is it new
    enough and does it actually offer every tool asked for. One runbook can
    collect more than one reason — a missing plugin and an undeclared tool
    are two different sentences, and both are worth showing at once.
    """
    reasons: list[str] = []

    for plugin, specifier in runbook.requires.plugins.items():
        version = facts.installed(plugin)
        if version is None:
            reasons.append(f"plugin {plugin} is not installed")
            continue
        if not facts.enabled(plugin):
            reasons.append(f"plugin {plugin} is off")
            continue
        if not facts.supports_runbooks(plugin):
            reasons.append(f"plugin {plugin} does not support runbooks")
            continue
        if not plugin_runbooks_on(plugin):
            reasons.append(f"runbooks are off for plugin {plugin}")
            continue
        try:
            spec_set = SpecifierSet(specifier)
        except InvalidSpecifier:
            # Schema validation already refuses an unparseable specifier
            # before a runbook can be stored, so this is unreachable in
            # practice — kept as a plain sentence rather than an assertion,
            # because "unreachable" is a claim about the caller, not a
            # guarantee this function can make about its own input.
            reasons.append(
                f"runbook {runbook.runbook} names an invalid version requirement "
                f"for {plugin}: {specifier!r}"
            )
            continue
        if not spec_set.contains(version, prereleases=True):
            reasons.append(f"{plugin} {version} installed, runbook needs {specifier}")

    for qualified in runbook.requires.tools:
        plugin, _, tool = qualified.partition(".")
        if not facts.declares_tool(plugin, tool):
            reasons.append(f"plugin {plugin} does not declare tool {tool}")

    return Verdict(ok=not reasons, reasons=reasons)


__all__ = ["PluginFacts", "Verdict", "check"]
