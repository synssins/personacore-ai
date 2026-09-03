# PersonaCore — CPU-only, non-root, appdata never baked in.
#
# CLAUDE.md's hard constraint: this image never relies on a GPU, for any reason.
# There is no CUDA/ROCm/SYCL layer here and no GPU device mapping in the Compose
# file. Anything that genuinely needs a GPU runs elsewhere and is reached over
# the network.
#
# Spec sections 7 and 10: the container is disposable and rebuildable; appdata is
# the assistant. Nothing under /appdata is COPYed in, so an upgrade cannot touch
# it — the strongest way to keep that promise is for the image to have no copy of
# it to overwrite.

# ---- build stage -----------------------------------------------------------
FROM python:3.12-slim AS build

# uv would be faster, but it is not on the approved dependency list, and pip is
# already here. Speed is not worth an unreviewed tool in the build path.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Build a wheel, then install it into a self-contained prefix the runtime stage
# copies wholesale. Keeps build tooling out of the shipped image.
RUN python -m pip install --upgrade pip build \
 && python -m build --wheel --outdir /dist \
 && python -m pip install --prefix=/install /dist/*.whl

# ---- model stage -----------------------------------------------------------
# The speech-to-text weights, fetched once and pinned by commit.
#
# A SEPARATE STAGE ON PURPOSE. Editing a Python file must not re-download 67 MB,
# and this layer's only inputs are the three ARGs below, so it is cached until
# one of them changes.
#
# THE MODEL IS NOT APPDATA. Appdata is the assistant — the operator's config,
# personas and voices — it lives in a mounted volume and an upgrade must never
# touch it, which is why nothing under /appdata is COPYed into this image. A
# model is the opposite: it is part of the build, identical in every container
# from a given tag, and replaced wholesale by pulling a new one. It belongs on
# the read-only side, and putting it here is what keeps those two facts from
# arguing.
#
# ONCE BUILT, THIS CONTAINER NEVER DOWNLOADS ANYTHING. The weights are in the
# image, so an air-gapped host starts and transcribes with no network at all.
# A model fetched at runtime would break that and would break the promise that
# nothing leaves the house.
FROM python:3.12-slim AS models

# moonshine-ai/moonshine, pinned by commit rather than `main` so the image is
# reproducible and cannot silently change under us. The vendor's own repository
# rather than a community mirror: its LICENSE is MIT for code and weights, and
# it is the one whose licence can be pointed at. (The two .onnx files are
# byte-identical to onnx-community/moonshine-base-ONNX.)
#
# Moonshine base, quantised: 20.5 MB encoder + 42.5 MB decoder + 3.8 MB
# tokeniser = 66.8 MB. Chosen over Whisper because its encoder is
# variable-length — Whisper zero-pads every clip to 30 seconds, so a 4-second
# dictation costs the same encode as a 25-second one.
ARG MOONSHINE_REPO=moonshine-ai/moonshine
ARG MOONSHINE_REVISION=48b4e427b587bcf67797a5be706d6ddc4a298149
ARG MOONSHINE_DIR=/opt/personacore/models/moonshine-base

# Downloaded with urllib rather than curl so the stage needs no apt layer, and
# checksummed against figures measured from the pinned revision. A mismatch
# FAILS THE BUILD LOUDLY: a silently substituted model would still load, still
# run, and return plausible nonsense, which is the worst failure mode available
# here. Verified before anything is moved into place, so a bad download cannot
# leave a half-written model behind.
RUN python - <<'PY'
import hashlib, os, pathlib, sys, time, urllib.request

repo = os.environ.get("MOONSHINE_REPO") or "moonshine-ai/moonshine"
rev = os.environ.get("MOONSHINE_REVISION") or "48b4e427b587bcf67797a5be706d6ddc4a298149"
out = pathlib.Path(os.environ.get("MOONSHINE_DIR") or "/opt/personacore/models/moonshine-base")

# name -> (path in repo, sha256, size in bytes)
WANTED = {
    "encoder_model.onnx": (
        "onnx/merged/base/quantized/encoder_model.onnx",
        "1dd9ab0a7f987113d30affcba5a068d11c8f90fa0223caa3e491ade431ad9751",
        20513063,
    ),
    "decoder_model_merged.onnx": (
        "onnx/merged/base/quantized/decoder_model_merged.onnx",
        "cc9f3cd6698a369c6008b41aa60aa3fb3322e7f03c9bdf19d8e6b7200afca4f3",
        42498870,
    ),
    "tokenizer.json": (
        "onnx/merged/base/float/tokenizer.json",
        "ad3a2ceb0e84e4da57451d86fa337f8116dcff5f5d106434f8aa0b0de89718b9",
        3761751,
    ),
}

staging = out.parent / (out.name + ".incoming")
staging.mkdir(parents=True, exist_ok=True)
for name, (path, want_sha, want_size) in WANTED.items():
    url = f"https://huggingface.co/{repo}/resolve/{rev}/{path}"
    print(f"fetching {name} from {url}", flush=True)
    target = staging / name
    # Retried, because this is a network read of ~66 MB from a host we do not
    # run, and a reset partway through is ordinary rather than exceptional. It
    # cost two releases -- v0.9.3 and v0.9.5 both died here on "connection
    # reset by peer" and published nothing, so the tag existed and the
    # container did not. The sha256 below is what makes retrying safe: a
    # truncated or substituted file still cannot pass.
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(url, timeout=180) as response:  # noqa: S310
                target.write_bytes(response.read())
            break
        except Exception as exc:  # noqa: BLE001 - any network failure is retryable
            if attempt == 5:
                sys.exit(f"FATAL: {name} could not be fetched from {url}: {exc!r}")
            wait = 2 ** attempt
            print(f"  attempt {attempt} failed ({exc!r}); retrying in {wait}s", flush=True)
            time.sleep(wait)
    size = target.stat().st_size
    got_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    if size != want_size or got_sha != want_sha:
        sys.exit(
            f"\nFATAL: {name} is not the file this image was built against.\n"
            f"  expected {want_size} bytes, sha256 {want_sha}\n"
            f"  got      {size} bytes, sha256 {got_sha}\n"
            f"  from     {url}\n"
            "A substituted model would load, run, and return confident nonsense, "
            "so the build stops here."
        )
    print(f"  ok {name}: {size} bytes, sha256 verified", flush=True)

staging.rename(out)
print(f"moonshine base ready in {out}", flush=True)
PY

# ---- runtime stage ---------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="PersonaCore" \
      org.opencontainers.image.description="Self-hosted, security-first agentic assistant. CPU-only." \
      org.opencontainers.image.source="https://github.com/synssins/personacore-ai" \
      org.opencontainers.image.licenses="MIT"

# Which build is this, exactly. The version alone cannot tell two builds of
# the same tag apart, and this image is pulled from a floating `latest`, so
# "what am I actually running" needs an answer a rebuild changes. Empty when
# built without them, and the UI shows nothing rather than guessing.
ARG BUILD_COMMIT=""
ARG BUILD_DATE=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PERSONACORE_APPDATA=/appdata \
    PERSONACORE_BUILD_COMMIT=${BUILD_COMMIT} \
    PERSONACORE_BUILD_DATE=${BUILD_DATE}

# Speech (ADR-0029). Core owns voice, so the machinery lives in this image and
# each engine is a switch over it — an engine nobody turns on costs disk and
# nothing else. Two things are needed and neither is a Python package:
#
#   espeak-ng   the phonemiser every CPU speech engine here fronts. GPL-3.0,
#               approved. ~12 MB with its language data, estimated: this
#               Dockerfile is not built on the dev host.
#   libgomp1    OpenMP, which the onnxruntime CPU wheel links against. Not a
#               dependency decision — it is that wheel's own shared library, and
#               without it `import onnxruntime` fails at load with an unhelpful
#               message about a missing .so.
#
# NO GPU, EVER (CLAUDE.md). Nothing here installs CUDA, ROCm or SYCL, and the
# Python side pins `onnxruntime`, never the `-gpu` distribution.
RUN apt-get update \
 && apt-get install --no-install-recommends -y espeak-ng libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Spec section 7: container images run as non-root users. Fixed uid/gid so the
# appdata volume's ownership is predictable when an operator sets it up.
RUN groupadd --gid 10001 personacore \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin personacore

COPY --from=build /install /usr/local
COPY deploy/entrypoint.py /usr/local/bin/entrypoint.py

# The reference plugin and the template ship with the core (spec section 5.1:
# "part of the core deliverable, not an extra"). Read-only here; the core copies
# them into appdata on first run, and never overwrites what is already there.
COPY plugins /opt/personacore/plugins

# Hearing. The speech-to-text weights, root-owned and read-only to the runtime
# user, at the path `personacore.hearing.recognisers.moonshine` looks in by
# default. Override with PERSONACORE_MOONSHINE_DIR to point at a tree outside
# the image; the recogniser reports itself unavailable, with a sentence naming
# the directory and the missing files, rather than failing to start if they are
# not there — so an image built without this COPY still boots and still runs
# every other part of the core.
#
# Same runtime as speech: onnxruntime, CPU only, already installed above. This
# adds 67 MB of weights and no new dependency, no second inference engine and
# no GPU anything.
COPY --from=models /opt/personacore/models /opt/personacore/models

# The mount point exists and is owned by the runtime user, but stays EMPTY in the
# image. On first run the core creates the layout inside whatever volume is
# mounted here (spec Appendix B).
RUN install -d -o 10001 -g 10001 /appdata

# Deliberately NOT `USER 10001` here. The entrypoint starts as root purely to
# take ownership of the appdata mount — a bind-mounted host directory arrives
# owned by whoever created it, and an already-unprivileged process cannot chown
# its way in. It drops to PUID:PGID (default 10001) before exec'ing the server,
# so nothing that opens a socket ever runs as root. Set `user:` in Compose to
# skip the chown entirely and stay unprivileged from PID 1.
WORKDIR /

EXPOSE 8053

# One port serves both surfaces on different paths: the OpenAI-compatible API at
# /v1 (spec section 5.4) and the admin API at /admin (section 9). TLS terminates
# at the reverse proxy (section 7), so this listener is plain HTTP.
#
# Host and port are environment-driven because host networking leaves no port
# mapping to resolve a clash with — the process must bind somewhere already free
# on the host. 8053 is the default only because it is unused; override it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD sh -c 'python -c "import os,sys,urllib.request; p=os.environ.get(\"PERSONACORE_PORT\",\"8053\"); sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{p}/health\", timeout=4).status==200 else 1)"'

# exec so the process becomes PID 1 and gets SIGTERM directly. Without it the
# shell swallows the signal and every stop waits out the kill timeout.
ENTRYPOINT ["python", "/usr/local/bin/entrypoint.py"]
CMD ["python", "-m", "personacore", "serve"]
