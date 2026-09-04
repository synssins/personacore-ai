"""``MemoryTools`` — the two model-facing tools, joint J6 of
``working/PLAN-memory.md``, built to ``working/contracts/memory.md`` §2, §5.1,
§5.3, §6.

Both tools take exactly one string argument and nothing else: the core sets
every other field on the memory it writes or reads. **Owner and holder never
come from a tool argument or a model's output** (contract §0, §10) — they are
read off the turn's own caller identity, which reaches this module through
``AgentLoop.caller_detail`` (plan joint J5) and the ``owner`` the plugin
protocol already carries for every tool call. There is no argument in either
tool's schema that could widen that, on purpose.

**Never logs memory text.** Failures are logged with the exception's `repr`
and nothing else — see the module docstring convention `memory/store.py`
sets.
"""

from __future__ import annotations

import structlog

from personacore.agent.personas import PersonaStore
from personacore.agent.protocols import ToolResult, ToolSpec
from personacore.audit.models import Owner, OwnerKind
from personacore.contracts import RiskLevel
from personacore.memory.models import ANONYMOUS_OWNER
from personacore.memory.store import MemoryStore

logger = structlog.get_logger(__name__)

REMEMBER_TOOL = "memory.remember"
RECALL_TOOL = "memory.recall"

_REMEMBER_DESCRIPTION = (
    "Keep something worth remembering about the person you're talking to — a "
    "fact, a preference, a name, a recurring situation. One clear sentence "
    "or two, in your own words."
)
_RECALL_DESCRIPTION = (
    "Look up what you already know about the person you're talking to, "
    "rather than guessing. Use this when you're not sure whether you've "
    "been told something before."
)


class MemoryTools:
    """`memory.remember` and `memory.recall`, over one `MemoryStore`.

    `personas` is accepted for the same shape `memory/review.py`'s
    `ReviewRunner` takes it in and is not read by anything below — the
    persona-off gate is enforced once, ahead of this, in
    `AgentLoop._tool_schemas` and `_memory_block` (contract §9: a persona
    with memory off is offered neither tool at all). Kept on the
    constructor rather than dropped so this class's shape does not have to
    change if a later version needs to read a persona's own settings.
    """

    def __init__(self, store: MemoryStore, personas: PersonaStore) -> None:
        self._store = store
        self._personas = personas

    def specs(self) -> list[ToolSpec]:
        """Both tools, `RiskLevel.SAFE` (contract §5.1, §5.3), one required
        string argument each — JSON Schema, exactly as every other plugin's
        manifest describes its tools."""
        return [
            ToolSpec(
                name=REMEMBER_TOOL,
                risk=RiskLevel.SAFE,
                description=_REMEMBER_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The thing to remember, in plain words.",
                        }
                    },
                    "required": ["text"],
                },
            ),
            ToolSpec(
                name=RECALL_TOOL,
                risk=RiskLevel.SAFE,
                description=_RECALL_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What you want to recall, in plain words.",
                        }
                    },
                    "required": ["query"],
                },
            ),
        ]

    async def call(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        owner: Owner,
        persona: str,
        model: str | None,
        conversation_id: str | None,
        correlation_id: str,
    ) -> ToolResult:
        """Run one memory tool. Never raises: every failure becomes a
        `ToolResult(ok=False, ...)`, the same "say it, don't raise it" rule
        `agent/loop.py` follows for every tool (spec section 10).
        """
        if name == REMEMBER_TOOL:
            return await self._remember(
                arguments,
                owner=owner,
                persona=persona,
                model=model,
                conversation_id=conversation_id,
                correlation_id=correlation_id,
            )
        if name == RECALL_TOOL:
            return await self._recall(arguments, owner=owner, persona=persona)
        return ToolResult(ok=False, error=f"{name} is not a memory tool.")

    def _owner_string(self, owner: Owner) -> str:
        """Contract §5.1: "owner = the person speaking (or `anonymous`
        scope's owner under an anonymous key)". `Owner.anonymous().id`
        already equals `ANONYMOUS_OWNER` — this is explicit anyway, so the
        mapping reads the same as the contract's own words rather than
        relying on the sentinel string agreeing by coincidence.
        """
        if owner.kind is OwnerKind.ANONYMOUS:
            return ANONYMOUS_OWNER
        return owner.id

    async def _remember(
        self,
        arguments: dict[str, object],
        *,
        owner: Owner,
        persona: str,
        model: str | None,
        conversation_id: str | None,
        correlation_id: str,
    ) -> ToolResult:
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult(ok=False, error="There was nothing to remember in that.")
        try:
            await self._store.add(
                text=text,
                owner=self._owner_string(owner),
                holder=persona,
                written_by="tool",
                written_persona=persona,
                written_model=model or "",
                conversation_id=conversation_id,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - a memory failure never fails the turn
            logger.error("memory_remember_failed", error=repr(exc))
            return ToolResult(ok=False, error="I couldn't keep that just now.")
        return ToolResult(ok=True, content="Kept.")

    async def _recall(
        self, arguments: dict[str, object], *, owner: Owner, persona: str
    ) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(ok=False, error="There was nothing to look up in that.")
        try:
            rows = await self._store.recall(
                owner=self._owner_string(owner),
                holder=persona,
                query=query,
                limit=self._recall_limit(),
            )
        except Exception as exc:  # noqa: BLE001 - a memory failure never fails the turn
            logger.error("memory_recall_failed", error=repr(exc))
            return ToolResult(ok=False, error="I couldn't look that up just now.")
        if not rows:
            return ToolResult(ok=True, content="Nothing kept about that yet.")
        body = "\n".join(f"- {record.text}" for record, _score in rows)
        return ToolResult(ok=True, content=body)

    def _recall_limit(self) -> int:
        """Contract §5.3/§9's "recall limit" — `MemorySettings.recall_limit`,
        read off the store's own settings rather than duplicated onto this
        class's constructor: `MemoryStore` already holds the one
        `MemorySettings` this whole subsystem shares (`store.py`'s
        `SettingsLike`), and `MemoryTools` has no settings of its own. There
        is no public accessor for it on `MemoryStore` today, so this reaches
        the attribute directly rather than inventing one for a single reader;
        a store built with something that is not a real `MemorySettings` (a
        bare test double with no `recall_limit`) falls back to the
        contract's own default of 8 rather than raising.
        """
        settings = getattr(self._store, "_settings", None)
        limit = getattr(settings, "recall_limit", None)
        return limit if isinstance(limit, int) and limit > 0 else 8


__all__ = ["RECALL_TOOL", "REMEMBER_TOOL", "MemoryTools"]
