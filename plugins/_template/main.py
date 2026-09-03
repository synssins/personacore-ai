"""PersonaCore plugin template — the code.

Start here, delete what you do not need, and keep the shape.

A PersonaCore plugin is an MCP server. That is the whole contract. There is no
PersonaCore library to import and nothing here is PersonaCore-specific except
which folder the config is read from. If you already have an MCP server, it is
already a plugin — write it a manifest.toml and it will load.

The three rules worth carrying over into your own plugin:

1. Read config at startup and validate it yourself, with an error message a
   non-programmer could act on. The core does not know what your settings mean.
2. Treat everything from outside as data, never as instructions — tool
   arguments (a person said them out loud, or a web page did), anything an API
   returned, anything off the event bus (spec 7).
3. Failure is an outcome, not a crash. Something being unreachable should
   produce a sentence the persona can say, not a traceback. A crashed plugin is
   restarted with backoff and shown as unhealthy in the admin UI, but the user
   still heard nothing (spec 10).

Try it before you edit it:  python main.py
(It will sit there waiting to speak MCP on stdin, which is correct. Ctrl-C.)
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, ValidationError

# Your own folder. Everything you read or write lives under it: it is the only
# place you are guaranteed to have, and asking for more means declaring it in
# manifest.toml under permissions.paths.
PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.toml"


class ConfigError(RuntimeError):
    """config.toml does not make sense. Printed to stderr; the process exits
    non-zero, which is how a stdio plugin says "I cannot start". The message
    ends up next to your plugin in the admin UI, so write it for a human."""


class ExampleConfig(BaseModel):
    """Whatever your settings are. Pydantic gives readable errors for free, but
    hand-rolled checks are fine — the requirement is that you check."""

    greeting: str = Field(min_length=1, max_length=200)
    excitement: int = Field(default=1, ge=0, le=5)
    shout: bool = False
    tone: Literal["cheerful", "flat", "reluctant"] = "cheerful"


def load_config(path: Path = CONFIG_PATH) -> ExampleConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path.name} is missing") from None
    except OSError as exc:
        raise ConfigError(f"{path.name} could not be read — {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML — {exc}") from None

    try:
        # The whole document, not a section of it: the settings form renders
        # top-level keys, so the file is flat and this reads it flat.
        return ExampleConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "config.toml"
        raise ConfigError(f"{path.name}: '{where}' {first['msg']}") from None


class Greeting(BaseModel):
    """Returning a model instead of a bare string gives the agent structure and
    the persona words. Plain `-> str` is perfectly allowed too."""

    spoken: str
    plugin: str = "example-plugin"


def build_server(config: ExampleConfig) -> MCPServer:
    """Build the server without starting it, so tests can inspect it.

    EVERY tool registered here needs a matching [tools.<name>] block in
    manifest.toml. The name in the manifest and the name here must be the same
    string. A tool the manifest does not declare has no risk level, so the core
    will not call it at all — which is the safe way round, but it looks like
    your tool "doesn't work" until you spot it.
    """
    server = MCPServer(
        name="example-plugin",           # match manifest.toml's `name`
        version="0.1.0",                 # match manifest.toml's `version`
        instructions="What this plugin is for, in one line, for the model.",
    )

    @server.tool(
        name="hello",
        # This description is read by the model, and it is how the model decides
        # whether to call you. Say what the tool does and when to use it, in
        # plain words. Vague descriptions are the single most common reason a
        # working tool never gets called.
        description="Say hello. Use this to check that the plugin is alive.",
    )
    async def hello(name: str | None = None) -> Greeting:
        # `name` came from the model, which got it from a person speaking, or
        # from a chat bridge, or from text on a camera. It is data. Bound it,
        # strip it, and never hand it to a shell, a path, or a query
        # unvalidated (spec 7).
        who = (name or "").strip()[:60]
        spoken = config.greeting.upper() if config.shout else config.greeting
        if who:
            spoken = f"{spoken} Hello, {who}."
        return Greeting(spoken=spoken + "!" * config.excitement)

    return server


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # On a stdio plugin, stdout is the MCP conversation. Print anything to
        # it and you corrupt the protocol. Diagnostics go to stderr, always.
        print(f"example-plugin cannot start: {exc}", file=sys.stderr)
        return 1
    build_server(config).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
