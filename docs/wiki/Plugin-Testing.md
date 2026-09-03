# Plugin Testing

How to test a plugin: what the bundled plugins' own tests do, how to run yours against the real discovery scanner and the real host, and the rule that keeps the whole suite honest. Read this before you write your first test.

Source: `tests/plugins_bundled/`, `tests/plugins/`.

## The no-network rule

**No test in the plugin suites opens a socket.** It is stated at the top of both `conftest.py` files and it is not decoration:

- `tests/plugins_bundled/conftest.py`: *"No test in this package makes a network call. The weather plugin's only outward reach is through `new_client()`, which is replaced with an `httpx.MockTransport` wherever it is exercised."*
- `tests/plugins/conftest.py`: *"No network, ever. Nothing in this package opens a socket. The HTTP transport is exercised through the same fake session the stdio one is, because what the host promises is that the two are indistinguishable to a caller — which is a statement about the host, not about `httpx`."*

A test that reaches the internet is a test that fails on a train, passes when the upstream API is having a good day, and tells you nothing about your code either way. Build your plugin so the seam exists: **one function that builds the HTTP client**, and nothing else constructing one.

## Three things worth building for testability

These are structural choices in the bundled plugins, made so the tests below are possible at all. Copy them.

**1. Split `build_server(config)` from `main()`.**

```python
def build_server(config: WeatherConfig) -> MCPServer: ...

def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"weather plugin cannot start: {exc}", file=sys.stderr)
        return 1
    build_server(config).run("stdio")
    return 0
```

It costs nothing and it means your tools can be inspected and called without spawning a subprocess.

**2. `load_config(path)` takes a path.** Defaulting to your own folder is fine; accepting an argument is what lets a test point it at a fixture.

**3. One function builds the HTTP client.**

```python
def new_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)
```

One place to swap in a mock transport, one place where the timeout and redirect policy are stated.

## Testing your plugin in isolation

A plugin is a standalone script, not an importable package — the core runs it as `python main.py` in its own folder. The bundled tests import it by path:

```python
def load_plugin_module(directory: Path, module_name: str) -> ModuleType:
    path = directory / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
```

Give each one a distinct module name — several plugins are called `main`.

### Serving fake upstream responses

```python
def serve(weather_module, monkeypatch, handler):
    def fake_client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    monkeypatch.setattr(weather_module, "new_client", fake_client)
```

Then a handler per scenario: a good payload, a 500, a `ConnectError`, a body that is not JSON, a body that is absurdly large, a payload with ragged arrays and wrong types. The weather tests do all of these, because the point of the plugin's parsing code is that a hostile payload cannot do more than shorten the forecast.

**Test the unhappy path as hard as the happy one.** Failure is an outcome, not a crash: assert that an unreachable service produces `available=False` and a speakable sentence, not an exception.

## Testing against the real contract

This is the part that catches drift, and it is the part most plugin authors skip.

### The manifest really validates

```python
raw = tomllib.loads((directory / "manifest.toml").read_text(encoding="utf-8"))
manifest = PluginManifest.model_validate(raw)
```

The real schema from `personacore.contracts.manifest`, not a copy of your understanding of it. If the contract changes under you, this fails in your test run rather than in someone's appdata.

### Discovery really loads it

Install it the documented way — copy the folder into `<appdata>/plugins/<name>/` — and run the real scanner:

```python
@pytest.fixture
def installed_plugins(tmp_path: Path) -> Path:
    plugins = tmp_path / "plugins"
    plugins.mkdir(parents=True)
    skip_junk = shutil.ignore_patterns("__pycache__")
    shutil.copytree(WEATHER_DIR, plugins / "weather", ignore=skip_junk)
    return tmp_path

def test_it_loads(installed_plugins: Path) -> None:
    result = PluginDiscovery(installed_plugins).scan()
    assert [f.message for f in result.failures] == []
    assert set(result.by_name()) == {"weather"}
```

Asserting `[f.message for f in result.failures] == []` rather than `result.failures == []` means a failure prints the sentence that explains it.

Note what the fixture has to do: the template ships as `_template`, so installing it means copying it to a folder named after the plugin — `example-plugin` — and that is exactly what the author guide tells you to do.

### The manifest and the server agree on the tool list

The check the core will make at load time, made in your test suite instead:

```python
server = module.build_server(module.load_config(directory / "config.toml"))
exposed = {tool.name for tool in await server.list_tools()}
assert exposed == set(manifest.tools)
```

A mismatch is a **terminal** load failure in production ([Plugin Contract](Plugin-Contract)). Catching it here costs one assertion.

### Code and manifest agree on the hosts

```python
assert set(manifest.permissions.network) == set(weather_module.NETWORK_HOSTS)
assert weather_module.API_URL.startswith(f"https://{weather_module.API_HOST}/")
```

The manifest is what a reviewer reads; the constant is what the code does. Since the core does not enforce network permissions for stdio plugins ([Manifest](Plugin-Manifest)), **this test is the only thing keeping the declaration true.**

### Risk levels are asserted, not assumed

```python
assert manifest.risk_of("get_forecast") is RiskLevel.SAFE
assert manifest.risk_of("search_locations") is RiskLevel.SAFE
```

The second one matters because a config-form lookup requires a `safe` tool (ADR-0016). A change of risk there would not be a security hole — the core refuses to wire it — it would be a search box that silently vanishes from the settings page, which is a defect nobody would think to look for.

The bundled suite goes further and checks every schema-declared lookup against the manifest: the tool must exist, be `safe`, and the `fill` map's keys must be real properties of the thing being filled.

### Constraints the plugin depends on are asserted too

```python
assert schema["properties"]["locations"]["minProperties"] == 1
```

The weather plugin refuses to start without a location, so the form must not be the thing that empties the list. `minProperties` is how the core knows — and this assertion is what stops someone deleting it during a tidy-up.

## Testing against the real host

`tests/plugins/` tests the core's side, and its structure is worth understanding because it shows what a plugin has to survive.

**Fakes first, one real subprocess.** Crashes, hangs, floods and refusals are far easier to produce — and far faster to run — from a fake session than from a real child process. `FakeFactory` takes a per-plugin plan: a callable receiving the attempt number and returning a session, or raising to model a plugin that will not start. That is how the backoff, restart, contract-reconciliation and containment paths get tested without a subprocess in sight.

`write_plugin()` writes a manifest-only plugin folder — no code at all, since the fake factory stands in for the server. Useful if you are testing the core rather than a plugin.

**Then one real end-to-end test.** `test_host_stdio.py` launches the bundled template over the real MCP stdio transport, through the real `PluginDiscovery` and the real `PluginHost`:

```python
host = PluginHost(
    PluginDiscovery(appdata),
    secrets=secrets,
    session_factory=McpSessionFactory(secrets=secrets, request_timeout=30.0),
    config=PluginHostConfig(supervisor=REAL),
)
await host.start()
health = {row.name: row for row in host.health()}["example-plugin"]
assert health.state is PluginState.HEALTHY, health.last_error
result = await host.call_tool("example-plugin.hello", {"name": "Ada"})
assert result.ok is True, result.error
```

Without it the whole suite could be internally consistent and wrong.

Two details worth copying if you do the same:

- **Generous timeouts.** `startup_timeout=60.0` in the test config. A real interpreter start plus the `mcp` import is not instant, especially on a cold Windows runner, and a flaky timeout here teaches people to ignore the file.
- **Skip when the interpreter is missing.** `pytest.mark.skipif(shutil.which("python") is None, ...)` — the bundled manifests start plugins with `python main.py`.

The same file proves the [environment boundary](Plugin-Runtime-Environment) from *inside* a child process, using a probe plugin that reports environment variable **names only** — never a value, so no secret is written anywhere by the test — and asserts the declared secret is present while an undeclared one, the core's API key, and every `PERSONACORE_*` variable are not.

And it proves containment: a plugin that prints to stderr and exits non-zero ends up `degraded` or `failed` with its own sentence in `last_error`, `list_tools()` returns nothing, and calling it anyway returns `ok=False` rather than raising.

## A checklist for your own plugin

- [ ] Manifest validates against the real `PluginManifest`.
- [ ] Real `PluginDiscovery` loads it from a copied folder with **zero failures**.
- [ ] `build_server(load_config(...))` exposes exactly the tools the manifest declares.
- [ ] Every risk level asserted, especially any tool a config lookup nominates.
- [ ] Hosts in the code match `permissions.network`.
- [ ] Config validation produces a readable message for each way it can be wrong.
- [ ] The unreachable-dependency path returns words, not an exception.
- [ ] Every outbound call goes through one swappable client factory, and no test opens a socket.

## See also

[Plugin Contract](Plugin-Contract) · [Manifest](Plugin-Manifest) · [Lifecycle](Plugin-Lifecycle) · [Weather walkthrough](Plugin-Walkthrough-Weather) · [Template walkthrough](Plugin-Walkthrough-Template)
