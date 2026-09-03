# Scenario: First Run

You have started the container for the first time and want to get from "it is running" to "it answered me" without guessing.

Assumes the stack is up — see [Installation and Upgrades](Installation-And-Upgrades) if it is not.

## 1. Confirm it is alive and which doors are open

```bash
curl -s http://127.0.0.1:8053/health
```

`/health` is the one endpoint that needs no authentication and is always served, even when a surface failed to mount. Read four fields:

- **`surfaces`** — should contain `admin` and `openai`. If one is missing, the container is running but that half of the product is not; the startup log will say `surface_absent` or `surface_mount_failed` with the reason.
- **`admin_auth`** — which single door is open. `method` is what is in force, `chosen` is what the settings say, and `setup_required` tells you the core's own sign-in has no account yet.
- **`admin_auth_bypass`** — `null` in any sane deployment. If it names a user, see step 2.
- **`trusted_proxies`** — who is allowed to tell the core who you are under the `proxy` door. Defaults to loopback.

Full field-by-field detail is in [Health and Diagnostics](Health-And-Diagnostics).

## 2. Get into the admin surface

**Exactly one way in is open at a time**, and which one is `[auth] method` in `core.toml` (ADR-0023, ADR-0024). Whichever it is, it fails **closed**: no credential is a 401 or a redirect to sign in, never an anonymous session.

A fresh stack uses **the core's own accounts** — `[auth] method` defaults to `builtin` (ADR-0024). So opening `http://<your-host>:8053/admin/` sends you to `/admin/setup`, a page that creates the first account. That account is an admin. There is no default password: you choose one here and nowhere else.

That is all most households need. Two other routes exist.

**A login proxy instead**, if you already run one: set `[auth] method = "proxy"` under Sign-in on the Core settings screen and restart. [Scenario: Putting Authelia in Front](Scenario-Putting-Authelia-In-Front) is the whole procedure. Only do this if the proxy is actually there — without one, nobody can get in.

**The development bypass**, for a machine nobody else can reach, or for getting back in when a password is lost. It outranks whichever door is configured.

```bash
docker compose stop personacore
```

Add to the `personacore` service's `environment:` in `compose.yaml`:

```yaml
      - PERSONACORE_ADMIN_DEV_USER=admin
```

```bash
docker compose up -d personacore
```

**Understand what you just did.** With this set, every request to the admin API is treated as coming from that user — the header is overwritten rather than honoured, so it is not a way to choose *which* user you are, but **anyone who can reach this port is now an admin**. It wins over `[auth] method`, whichever way that is set; `/health` reports `admin_auth.chosen` so you can see which door you get back when you remove it. It exists because an unusable admin surface is how a security control gets ripped out rather than configured. It logs a warning at startup and it shows up in `/health` as `admin_auth_bypass`, precisely so it cannot quietly survive into a deployment that has a proxy. Unset it the moment you have another way in. See [Security Model](Security-Model).

Now `http://<your-host>:8053/admin/` opens — it redirects to the Chat screen at `/admin/chat`. The sidebar has Chat, Health, Model connections, Plugins, Personas, Speech engines (with a Voices page under it), Core settings, a Security group holding Access keys and Users and sessions, and Logs.

## 3. Look at what first run created

Under your appdata directory you now have the full tree, plus three things the core wrote because a container never runs an `init` command:

- `config/core.toml` — a commented starter file.
- `personas/default/system_prompt.md` — a starter persona.
- `plugins/weather/` and `plugins/_template/` — the bundled plugins, copied out of the image.

None of these is ever overwritten on a later start. The layout is documented in [Appdata Layout](Appdata-Layout).

**A common misreading at this point:** `plugins/_template/` is not a broken plugin. Folders whose names begin with `_` or `.` are not treated as plugins, which is exactly why the template can live in the plugins directory without the core trying to run it.

## 4. Point it at your LLM

The starter `core.toml` contains:

```toml
[llm.interactive]
base_url = "http://localhost:11434/v1"
model = "llama3.1:8b"
```

That is a placeholder, and `localhost` **inside a container is the container**, not your host — the starter file says so in a comment for exactly this reason. Until you change it, every turn will fail to connect.

Do it in the admin surface rather than by editing the file: the connection panel has a per-role form and a **test connection** action, and settings apply live with no restart. The full walkthrough, including using more than one role, is [Scenario: Connecting an LLM](Scenario-Connecting-An-LLM).

## 5. Say something to it

The Chat screen at `/admin/chat` is a real conversation, not a one-off diagnostic box: earlier turns in the same conversation are taken into account, a rail down the side lists your past conversations so you can resume one, and a persona picker sits right on this screen. Picking a persona there changes who answers *this* conversation — it does not move the core's default persona, so other clients are unaffected. There is also a microphone button that dictates using your browser's own speech recognition (not a core speech-to-text engine), and replies can be read aloud if a voice is set up. Send anything.

Below the reply you get a line reporting **how many tools were offered to the model and which it called**. Read it even when the reply looks fine — it is the single most useful diagnostic in the product, and it is there because "the assistant says it has no tools" has two completely different causes with opposite fixes. See [Scenario: Debugging a Plugin](Scenario-Debugging-A-Plugin).

(This screen does not yet show generation statistics such as tokens per second — that is designed but not built.)

What can go wrong here:

| What you see | What it means |
|---|---|
| A plain sentence about not reaching the model | The LLM endpoint is wrong or down. Failures are worded, never raised — a dead host reads as a sentence, not a traceback. Use the test-connection action. |
| An answer, and `0 tools offered` | No plugin is running yet, or all of them are disabled. Check the plugin list. |
| An answer, and `N tools offered … none called` | The tools reached the model and it chose not to use them. That is not a fault, and it is not something the core can fix — see below. |

## 6. Check the plugins came up

The plugin list in the admin surface shows every plugin that loaded, every one that failed with a plain-English reason, and live health for each.

The bundled **weather** plugin ships with a placeholder location (London) and works out of the box. Replace it with somewhere you care about from the plugin's settings page — there is a search box on the locations setting, so you type a town and pick it rather than finding coordinates yourself ([ADR-0016](https://github.com/synssins/personacore-ai/blob/main/docs/adr/0016-config-field-lookups.md)).

If a plugin shows as **stopped** with a sentence about being switched off, someone disabled it; that state lives in `config/plugins-disabled.toml` and survives restarts, by design.

## 7. The thing that will surprise you

**A model that does not support tool calling will politely tell you it cannot do things, even though the tools reached it.** This was found during the P0 phase gate against a real model host: the tool schema was captured on the outgoing request, the plugin answered a direct call with real data, and the model still declined and said it had no such ability.

That is the model, or its chat template, and not the pipeline. The fix is a tool-capable model in the `interactive` role — not a change to PersonaCore. The `N tools offered … none called` line is what lets you tell the two apart in one glance.

## Where to go next

- [Scenario: Connecting an LLM](Scenario-Connecting-An-LLM) — required before anything works.
- [Scenario: Installing a Plugin](Scenario-Installing-A-Plugin) — give it something to do.
- [Scenario: Third-Party Clients](Scenario-Third-Party-Clients) — point an existing chat client at it.
- [Scenario: Putting Authelia in Front](Scenario-Putting-Authelia-In-Front) — before this is reachable by anything you do not control.
