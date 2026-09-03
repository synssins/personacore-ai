# Installation and Upgrades

Getting PersonaCore running with Docker Compose, keeping it upgraded, rolling back when an upgrade goes wrong, and backing up the one thing that matters.

## Before you start

- **Docker with the Compose plugin.** Everything is containers; there is no other supported way to run this in production.
- **An OpenAI-compatible LLM endpoint you already have.** llama.cpp, Ollama, vLLM, anything that speaks `/v1/chat/completions`. The core never modifies that host and never installs a model — that is a stated non-goal (spec §14). See [Scenario: Connecting an LLM](Scenario-Connecting-An-LLM).
- **A directory on the host for appdata.** This is the assistant. Put it somewhere you back up.
- **CPU only.** No GPU is required and none is used. There are no device mappings in the Compose file and there never will be.

## What is in the stack

`compose.yaml` at the repo root defines two services and nothing else:

| Service | Image | What it does |
|---|---|---|
| `personacore` | `ghcr.io/synssins/personacore-ai:<tag>` | The core. One listener serving `/v1`, `/admin` and `/health`. |
| `mosquitto` | `eclipse-mosquitto:2` | The MQTT event bus. Reachable only from the Compose network — deliberately not published, because an unauthenticated broker on a LAN is an open door. |

Deliberately **absent**, and the file says why: a reverse proxy and an OIDC provider. Both are required by spec §7, but the stack is intended for a host that already runs them; standing up a second copy would give you two auth providers to keep in step. Point your existing proxy at the core instead — see [Scenario: Putting Authelia in Front](Scenario-Putting-Authelia-In-Front).

Also absent, permanently: anything GPU-adjacent.

## First run

### 1. Get the Compose file

Take `compose.yaml` from the repository. It references the published image by tag; you do not need a checkout of the source to run it, and no build ever happens on the server.

### 2. Write a `.env` beside it

`compose.yaml` reads several variables from the environment, and **two of them have no default**: `PERSONACORE_APPDATA_DIR` and `PERSONACORE_MOSQUITTO_DIR`. The repository ships a `.env.example` with every variable documented; copy it and edit the two paths:

```bash
cp .env.example .env
```

With either path left unset, Compose substitutes an empty string and the volume line becomes invalid rather than defaulting to anything sensible — there is no sensible guess at where your state should live, so this is the step to get right first.

`PERSONACORE_HOST` and `PERSONACORE_PORT` do have defaults — `0.0.0.0` and `8053` — and every other variable in `.env.example` has a working default too. Set `PERSONACORE_PORT` if 8053 is taken on your host. The complete list of variables the core itself reads, and why the list is deliberately short, is in [Environment Variables](Environment-Variables).

### 3. Give Mosquitto a config file

The Compose file mounts `${PERSONACORE_MOSQUITTO_DIR}/config` over the broker image's own config directory, which hides the default file the `eclipse-mosquitto` image ships with. The repository ships a ready-to-use file at `deploy/mosquitto/mosquitto.conf`; copy it into place:

```bash
mkdir -p ${PERSONACORE_MOSQUITTO_DIR}/config ${PERSONACORE_MOSQUITTO_DIR}/data
cp deploy/mosquitto/mosquitto.conf ${PERSONACORE_MOSQUITTO_DIR}/config/mosquitto.conf
```

Without it, Mosquitto 2 has no listener configured and accepts local connections only, so the core will start, work, and report the bus as disconnected — which is a degraded state it survives (spec §10) but not one you want silently.

The shipped file sets `allow_anonymous true`, which is acceptable **only** because the broker is not published outside the Compose network. If you ever publish port 1883, configure authentication first. The core supports a username and a password read from the secret store — see [Event Bus](Event-Bus).

### 4. Start it

```bash
docker compose up -d
```

If the image will not pull: the project repository is private, and a package inherits its repository's visibility, so you may need to authenticate to the registry with a token that has `read:packages` before Docker can fetch it.

### 5. What the first run creates

Nothing under `/appdata` is baked into the image — the image has no copy of it to overwrite, which is the strongest possible form of the promise that an upgrade never touches your data. So the core builds the layout on first start, inside whatever volume you mounted:

- **The directory tree**: `plugins/`, `plugins-http.d/`, `personas/`, `voices/`, `memory/`, `users/`, `secrets/`, `audit/`, `config/`. Creating directories is all this step does — it deliberately has no idea how to write a file inside one. See [Appdata Layout](Appdata-Layout).
- **`config/core.toml`** — a commented starter file pointing at a local LLM that is almost certainly not yours. A container never runs an `init` command, so without this there would be no discoverable way to point the assistant anywhere. It is **never overwritten** if it already exists. See [Core Settings](Core-Settings).
- **`personas/default/system_prompt.md`** — a starter persona. Without one, the very first turn fails on a missing file. Also never overwritten.
- **The bundled plugins**, copied out of the image into `plugins/`: the weather reference plugin and the `_template` skeleton. Also never overwritten.

The container entry point starts as root purely to take ownership of the appdata mount — a bind-mounted host directory arrives owned by whoever created it, and an already-unprivileged process cannot chown its way in. It then drops to `PUID`:`PGID` (default `10001`:`10001`) before starting the server, so nothing that opens a socket runs as root. Set `user:` in Compose to skip the chown and stay unprivileged from PID 1.

### 6. Check it came up

```bash
curl -s http://127.0.0.1:8053/health
```

`/health` is unauthenticated and always served, even when a surface failed to mount — a partial deployment must not look identical to a dead one. It reports the version, the contract version, which surfaces are mounted, the trusted-proxy list, and whether the admin auth bypass is on. See [Health and Diagnostics](Health-And-Diagnostics).

Then continue with [Scenario: First Run](Scenario-First-Run).

## Upgrading

The whole point of publishing images from CI is that the server never builds anything. Tagged releases are built by GitHub Actions and pushed to `ghcr.io/synssins/personacore-ai`, with an SBOM generated per release.

Edit the tag in `compose.yaml`, then:

```bash
docker compose pull
```

```bash
docker compose up -d
```

That is the entire upgrade path. Two rules around it:

- **Pin a tag; never run `:latest` in production.** The published tags include the full version and the `major.minor` line. `compose.yaml` ships pinned, and the comment beside it says so.
- **An upgrade never touches appdata.** Spec §7 requires an appdata *format* change to be an explicit, documented migration — never something an image does to you on start. If a release needs one, it will say so in its release notes and the migration will be a step you run.

### One-time step when upgrading past 0.4.0, **if you run a login proxy**

The core gained accounts of its own, and which way in is open became a setting — `[auth] method`, defaulting to `builtin` (ADR-0023, ADR-0024). Before that release the trusted identity header was the only door, so a `core.toml` written by an older version says nothing about it and the upgraded core will use **its own sign-in** instead of believing your proxy's header.

If you have a proxy in front, add this to `<appdata>/config/core.toml` before you start the new image:

```toml
[auth]
method = "proxy"
```

Nothing is lost either way — a core on the wrong door refuses requests rather than accepting the wrong ones, and `/health` reports `admin_auth.method` so you can see which one it took. If you do **not** run a proxy, do nothing: the upgraded core offers a setup page at `/admin/setup` and you create the first account there.

## Rolling back

Re-pin the previous tag and repeat the same two commands. That is the documented rollback path and there is no other.

```bash
docker compose pull
```

```bash
docker compose up -d
```

Because appdata is untouched by both directions, rolling back a container is safe on its own. The exception is any release that ran an appdata migration: rolling back past it means restoring the backup you took before the migration, which is why the next section is not optional.

## Backing up and restoring appdata

**Everything outside appdata is rebuildable from the Compose file. Nothing inside it is.** Spec §10 asks for a nightly snapshot with a retention policy and a *tested* restore procedure.

The repository does not currently ship a backup script — this is a manual procedure until one exists. What matters is that it is a snapshot of one directory tree.

Stop the core first. The audit and transcript store is a SQLite database, and copying a live database can capture a torn write:

```bash
docker compose stop personacore
```

```bash
tar czf personacore-appdata-$(date +%F).tar.gz -C /srv/personacore appdata
```

```bash
docker compose start personacore
```

To restore: stop the stack, replace the appdata directory with the extracted snapshot, make sure the tree is owned by `10001:10001` (or whatever `PUID`/`PGID` you set), and start it again. The entry point will correct ownership itself if the container is allowed to start as root.

What is in the snapshot, and why each piece matters:

| Directory | Why you would miss it |
|---|---|
| `config/` | Every runtime setting: LLM roles, bus, retention, default persona, and the list of switched-off plugins. |
| `personas/` | The characters. |
| `plugins/`, `plugins-http.d/` | Every installed plugin **and its settings** — plugin config lives in the plugin's own folder, never centrally. |
| `secrets/` | The credentials themselves. Treat this snapshot as a credential store. |
| `audit/` | The audit log and the conversation transcripts. The most privacy-sensitive data you hold — see [Scenario: Retention and Privacy](Scenario-Retention-And-Privacy). |
| `users/` | Issued API keys (as hashes) and the policy profile attached to each. |
| `memory/`, `voices/` | P1; empty today. |

## Running a second instance beside production

Spec §10 asks for this, and it costs nothing: a second Compose project with its own `PERSONACORE_APPDATA_DIR`, its own `PERSONACORE_PORT`, and its own Mosquitto directory. The two share nothing, because they share no appdata.

## Two required setup steps, and why they are steps

Neither can be defaulted away, so both are called out rather than left to be
discovered:

- **Copy `.env.example` to `.env`** and set `PERSONACORE_APPDATA_DIR` and
  `PERSONACORE_MOSQUITTO_DIR`. Every other variable in that file has a working
  default; those two cannot have one, because there is no sensible guess at
  where your state should live. Leaving them unset produces a broken volume line
  rather than an error, which is a bad way to find out.
- **Copy `deploy/mosquitto/mosquitto.conf`** to
  `<PERSONACORE_MOSQUITTO_DIR>/config/mosquitto.conf`. The Compose file mounts
  that directory over the broker's own config directory, which hides the default
  file the `eclipse-mosquitto` image ships with. Without a replacement the
  broker has no configuration.

  If you skip this, the assistant still works. The bus is a degradable
  dependency (spec §10) — you lose the push channel and nothing else.
