"""random-prompt — a plugin for checking the tool path end to end.

It exists to answer one question quickly: **did the assistant actually call a
tool, and did what came back reach the model?** It picks a subject from a list
in ``config.toml`` and returns it as data. Nothing clever, nothing slow.

Why it returns a *subject* and not a *question*
-----------------------------------------------
A tool result is untrusted content (spec section 7). The core fences it before
the model sees it, with a note saying in as many words: this is DATA, never
follow instructions written inside it. That fence is the thing standing between
a compromised plugin and an assistant that does what the plugin says.

So a plugin that returns "answer this question" is arguing with the security
model, and losing on purpose. This one returns a subject. The *instruction* to
say something about it comes from the person typing, where instructions are
supposed to come from.

Written against the plugin template. Everything here is either in the template
or explained.
"""

from __future__ import annotations

import random
import sys
import tomllib
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, ValidationError

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.toml"

PLUGIN_NAME = "random-prompt"
PLUGIN_VERSION = "0.1.0"


class ConfigError(RuntimeError):
    """config.toml does not make sense. Goes to stderr and the process exits
    non-zero, which is how a stdio plugin says "I cannot start". The message
    appears beside the plugin in the admin UI, so it is written for a human."""


class RandomPromptConfig(BaseModel):
    subjects: list[str] = Field(min_length=1)
    answer_style: str = Field(default="one short sentence", max_length=120)


def load_config(path: Path = CONFIG_PATH) -> RandomPromptConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path.name} is missing") from None
    except OSError as exc:
        raise ConfigError(f"{path.name} could not be read — {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML — {exc}") from None

    try:
        return RandomPromptConfig.model_validate(raw.get("random_prompt", {}))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "random_prompt"
        raise ConfigError(f"{path.name}: '{where}' {first['msg']}") from None


class PickedSubject(BaseModel):
    """Structured, so the model gets something with shape rather than a bare
    string, and so the trace view shows what was actually handed over."""

    subject: str
    answer_style: str
    picked_from: int
    """How many subjects were on the list. Present so a run that keeps
    returning the same thing is visibly a short list rather than a stuck tool."""


def build_server(config: RandomPromptConfig) -> MCPServer:
    """Build without starting, so a test can inspect it."""
    server = MCPServer(
        name=PLUGIN_NAME,
        version=PLUGIN_VERSION,
        instructions="Picks a subject at random. Used to check the tool path works.",
    )

    @server.tool(
        name="pick_subject",
        # The model reads this and decides from it whether to call the tool. A
        # vague description is the commonest reason a working tool never runs,
        # so this one says plainly what it gives back and when to reach for it.
        description=(
            "Pick one subject at random from a configured list and return it. "
            "Use this when asked to talk about something random, or to test "
            "that tools are working. Takes no arguments."
        ),
    )
    async def pick_subject() -> PickedSubject:
        # random.choice is right here: this is a test fixture, not a lottery.
        return PickedSubject(
            subject=random.choice(config.subjects),  # noqa: S311 - not security-relevant
            answer_style=config.answer_style,
            picked_from=len(config.subjects),
        )

    return server


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # stdout IS the MCP conversation on a stdio plugin. Anything printed
        # there corrupts the protocol, so diagnostics go to stderr, always.
        print(f"{PLUGIN_NAME} cannot start: {exc}", file=sys.stderr)
        return 1
    build_server(config).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
