"""A development command line — a way to drive the core without a browser.

This is **not** the product. Spec section 9's admin UI (now at ``/admin``) and
section 5.4's exposed API are the real surfaces. This exists so the core can be
exercised by a human from a terminal, with no browser and no server running —
the P0 acceptance criteria in section 12 are things somebody has to be able to
*try*.

It wires the pieces that exist and nothing more:

    appdata -> settings -> LLM client -> persona store -> agent loop -> stdout

No plugins yet, so no tools: the plugin host is still being built. What it does
prove is the part section 5.3 promises — that pointing the assistant at a
different OpenAI-compatible backend is a config change and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from personacore.agent.loop import (
    AgentEventType,
    AgentLoop,
    ConversationMessage,
    TurnRequest,
)
from personacore.agent.personas import DEFAULT_PERSONA_NAME, PersonaStore
from personacore.audit.logging import LoggingConfig, configure_logging
from personacore.audit.models import AuditStoreConfig, MessageRole, Surface
from personacore.audit.store import AuditStore
from personacore.config import AppdataLayout, SecretStore, load_core_settings
from personacore.config.settings import STARTER_CONFIG, LLMRole
from personacore.contracts.policy import PolicyProfile, ProfileKind, RiskLevel
from personacore.llm.client import LLMClient, LLMClientConfig

DEFAULT_APPDATA = Path("./appdata")

from personacore.agent.personas import STARTER_PERSONA  # noqa: E402


def _layout(args: argparse.Namespace) -> AppdataLayout:
    return AppdataLayout(args.appdata)


def cmd_init(args: argparse.Namespace) -> int:
    layout = _layout(args)
    layout.ensure()

    persona_dir = layout.personas / DEFAULT_PERSONA_NAME
    persona_dir.mkdir(parents=True, exist_ok=True)
    prompt = persona_dir / "system_prompt.md"
    if prompt.exists():
        print(f"persona   kept     {prompt} (already there, not overwritten)")
    else:
        prompt.write_text(STARTER_PERSONA, encoding="utf-8")
        print(f"persona   created  {prompt}")

    config = layout.core_config_file
    if config.exists():
        print(f"settings  kept     {config} (already there, not overwritten)")
    else:
        config.write_text(STARTER_CONFIG, encoding="utf-8")
        print(f"settings  created  {config}")

    print(f"appdata   ready    {layout.root}")
    print("\nEdit the [llm] section to point at your own host, then: personacore chat")
    return 0


async def _build(
    args: argparse.Namespace, *, quiet_logs: bool = False
) -> tuple[AppdataLayout, LLMClient, AgentLoop]:
    layout = _layout(args)
    if not layout.core_config_file.exists() and not layout.personas.exists():
        raise SystemExit(
            f"No appdata at {layout.root}. Run 'personacore init' first, "
            "or pass --appdata pointing at an existing volume."
        )
    settings = load_core_settings(layout)

    # Logs still go to the file under appdata; they just stop interleaving with
    # a streamed reply. A console where the answer arrives in pieces between log
    # lines is unreadable, and this console exists to be read.
    configure_logging(
        LoggingConfig(log_dir=layout.audit, to_stdout=not quiet_logs, level="INFO")
    )

    # The console is a conversation, so it is the `interactive` role (ADR-0011).
    # It never needs the other four, so it builds one client rather than a
    # roster; `llm_for` is still the only thing that knows about the fallback.
    interactive = settings.llm_for(LLMRole.INTERACTIVE)
    api_key = None
    if interactive.api_key_secret:
        # Core-owned (ADR-0025). The console reads it through the same named
        # door the server does — `SecretStore` has no value-returning method of
        # its own, and no plugin namespace can reach the core's.
        store = SecretStore(layout)
        # The console can be the first thing run against an appdata volume
        # upgraded across ADR-0025, so the flat secret has to be moved before it
        # is looked for. Idempotent, so it costs a directory listing when there
        # is nothing to move.
        store.migrate()
        api_key = store.core_secrets().get(interactive.api_key_secret)

    llm = LLMClient(
        LLMClientConfig(
            base_url=interactive.base_url,
            model=interactive.model,
            api_key=api_key,
            connect_timeout=interactive.connect_timeout_seconds,
            read_timeout=interactive.read_timeout_seconds,
            failure_threshold=interactive.failure_threshold,
            cooldown_seconds=interactive.cooldown_seconds,
        )
    )
    audit = AuditStore(AuditStoreConfig(database_path=layout.audit / "audit.db"))
    loop = AgentLoop(
        llm=llm,
        personas=PersonaStore(layout, default_persona=settings.default_persona),
        audit=audit,
    )
    return layout, llm, loop


async def _doctor(args: argparse.Namespace) -> int:
    layout, llm, _ = await _build(args)
    settings = load_core_settings(layout)
    print(f"appdata   {layout.root}")
    for role in LLMRole:
        endpoint = settings.llm_for(role)
        fallback = settings.llm.falls_back_to(role)
        suffix = f"  (falls back to {fallback.value})" if fallback else ""
        print(f"llm {role.value:<12} {endpoint.model} at {endpoint.base_url}{suffix}")
    personas = PersonaStore(layout, default_persona=settings.default_persona).available()
    print(f"personas  {', '.join(personas) if personas else '(none found)'}")
    try:
        health = await llm.health_check()
        print(f"reachable {health}")
        return 0
    except Exception as exc:  # noqa: BLE001 - a diagnostic tool reports, it does not raise
        print(f"reachable NO — {exc}")
        print("\nCheck the base_url in core.toml. It must include the /v1 suffix.")
        return 1
    finally:
        await llm.aclose()


def _profile(name: str) -> PolicyProfile:
    """The console user. No tools are connected yet, so the ceiling stays at
    'safe' — there is nothing here that should be able to unlock a door."""
    return PolicyProfile(
        id=name,
        display_name=name,
        kind=ProfileKind.USER,
        enabled=True,
        max_tool_risk=RiskLevel.SAFE,
        may_approve_confirm=True,
    )


async def _chat(args: argparse.Namespace) -> int:
    layout, llm, loop = await _build(args, quiet_logs=True)
    settings = load_core_settings(layout)
    profile = _profile(args.user)
    history: list[ConversationMessage] = []
    persona = args.persona or settings.default_persona

    print(
        f"PersonaCore — persona '{persona}', "
        f"model '{settings.llm_for(LLMRole.INTERACTIVE).model}'"
    )
    print("Type ':persona <name>' to switch, ':reset' to clear history, ':q' to quit.\n")
    try:
        while True:
            try:
                # Off the event loop: a blocking read here would stall the
                # streaming that this console exists to demonstrate.
                line = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line in (":q", ":quit", ":exit"):
                return 0
            if line == ":reset":
                history.clear()
                print("(history cleared)\n")
                continue
            if line.startswith(":persona "):
                persona = line.split(" ", 1)[1].strip()
                print(f"(persona -> {persona})\n")
                continue

            request = TurnRequest(
                user_message=line,
                profile=profile,
                surface=Surface.ADMIN_UI,
                history=list(history),
                persona_override=persona,
            )
            reply: list[str] = []
            print("core> ", end="", flush=True)
            async for event in loop.run_turn(request):
                if event.type is AgentEventType.TEXT_DELTA:
                    reply.append(event.text)
                    print(event.text, end="", flush=True)
                elif event.type is AgentEventType.NOTICE:
                    print(f"\n[{event.text}]", end="", flush=True)
                elif event.type is AgentEventType.TOOL_CALL:
                    print(f"\n[tool: {event.text}]", end="", flush=True)
            print("\n")
            history.append(ConversationMessage(role=MessageRole.USER, content=line))
            history.append(
                ConversationMessage(role=MessageRole.ASSISTANT, content="".join(reply))
            )
    finally:
        await llm.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="personacore",
        description="Development console for the PersonaCore core. Not the product.",
    )
    parser.add_argument(
        "--appdata",
        default=os.environ.get("PERSONACORE_APPDATA", DEFAULT_APPDATA),
        type=Path,
        help=f"appdata volume to use (default: {DEFAULT_APPDATA})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the appdata layout, a starter persona and settings")
    sub.add_parser("doctor", help="report what is configured and whether the LLM host answers")

    srv = sub.add_parser("serve", help="run the HTTP server (the container entry point)")
    srv.add_argument("--host", default=None, help="bind address (env PERSONACORE_HOST)")
    srv.add_argument("--port", type=int, default=None, help="port (env PERSONACORE_PORT)")

    chat = sub.add_parser("chat", help="talk to the assistant")
    chat.add_argument("--persona", default=None, help="persona to use for this session")
    chat.add_argument("--user", default="console", help="profile id to attribute the turn to")

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "doctor":
        return asyncio.run(_doctor(args))
    if args.command == "serve":
        from personacore.server import serve

        # host/port resolution (explicit arg > PERSONACORE_HOST/PORT > core.toml's
        # [server], ADR-0010) lives in serve() itself, so there is exactly one
        # place that order is implemented rather than two that can drift apart.
        serve(host=args.host, port=args.port, appdata=args.appdata)
        return 0
    return asyncio.run(_chat(args))


if __name__ == "__main__":
    sys.exit(main())
