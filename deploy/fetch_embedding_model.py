#!/usr/bin/env python3
"""Fetch the bundled MiniLM embedding model into personacore/memory/models/.

Pinned in working/contracts/memory.md section 4 and the memory plan's joint
J3: sentence-transformers/all-MiniLM-L6-v2, ONNX export, AVX2-quantised,
384 dimensions. stdlib only — urllib and hashlib — so this runs in the
Docker build stage before anything else is installed, and locally for
anyone standing up a dev environment.

Idempotent: a file already on disk with the right size and sha256 is left
alone, not re-downloaded. A mismatch — wrong size or wrong hash, whether
from a bad download or a substituted file — is refused loudly. A corrupted
or swapped model would still load, still run, and return confident
nonsense, which is the worst failure mode available here, so this stops
before anything is written into place.

Placement: this lives in ``deploy/`` rather than a repository-root
``scripts/`` directory. ``scripts/`` is already a name owned by the working
root one level up (test and QA tooling, deliberately never published — see
CLAUDE.md's repo-hygiene section) and, separately, this repository's own
``.dockerignore`` excludes any ``scripts/`` directory from the image build
context. This script has to be readable *during* the Docker build, so it
sits next to the Dockerfile and entrypoint.py that already live in
``deploy/``, with its own narrow ``.dockerignore`` exception.

Usage::

    python deploy/fetch_embedding_model.py [dest_dir]

``dest_dir`` defaults to ``src/personacore/memory/models`` next to this
script's own repository checkout.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

#: The pinned source. Reconfirmed against the pinned files by the tech lead
#: on 2026-09-03 (working/contracts/memory.md J3) — the model opens in
#: onnxruntime 1.2x with the expected input/output shapes.
REPO = "sentence-transformers/all-MiniLM-L6-v2"
REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"

#: filename -> (path inside the HF repo, sha256, size in bytes or None when
#: J3 does not pin a size for that file — the hash alone is still checked).
WANTED: dict[str, tuple[str, str, int | None]] = {
    "model_quint8_avx2.onnx": (
        "onnx/model_quint8_avx2.onnx",
        "b941bf19f1f1283680f449fa6a7336bb5600bdcd5f84d10ddc5cd72218a0fd21",
        23046789,
    ),
    "vocab.txt": (
        "vocab.txt",
        "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
        None,
    ),
}

#: src/personacore/memory/models, relative to this file's own repository
#: checkout — not the current working directory, so this script behaves the
#: same whether it is run from the working root, the repo root, or inside a
#: Docker build stage.
DEFAULT_DEST = Path(__file__).resolve().parent.parent / "src" / "personacore" / "memory" / "models"


def sha256_of(path: Path) -> str:
    """The file's sha256, read in chunks so a 23 MB model is not slurped whole."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, want_sha256: str, want_size: int | None) -> bool:
    """Whether ``path`` already matches the pinned size and hash.

    Used both to decide a re-download is unnecessary (idempotence) and to
    accept or refuse a freshly downloaded file — the same check either way,
    which is the point: there is exactly one definition of "the right file."
    """
    if not path.is_file():
        return False
    if want_size is not None and path.stat().st_size != want_size:
        return False
    return sha256_of(path) == want_sha256


def _fetch_one(
    name: str, repo_path: str, want_sha256: str, want_size: int | None, dest_dir: Path
) -> None:
    target = dest_dir / name
    if verify(target, want_sha256, want_size):
        print(f"ok {name}: already present, checksum verified")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{repo_path}"
    print(f"fetching {name} from {url}")
    staging = target.with_name(target.name + ".incoming")
    try:
        with urllib.request.urlopen(url, timeout=180) as response:  # noqa: S310
            staging.write_bytes(response.read())
    except Exception as exc:  # noqa: BLE001 - any network failure is fatal here
        staging.unlink(missing_ok=True)
        sys.exit(f"FATAL: {name} could not be fetched from {url}: {exc!r}")

    if not verify(staging, want_sha256, want_size):
        got_size = staging.stat().st_size
        got_sha256 = sha256_of(staging)
        staging.unlink(missing_ok=True)
        size_line = f", {want_size} bytes" if want_size is not None else ""
        sys.exit(
            f"FATAL: {name} is not the file this build is pinned to.\n"
            f"  expected sha256 {want_sha256}{size_line}\n"
            f"  got      sha256 {got_sha256}, {got_size} bytes\n"
            f"  from     {url}\n"
            "A substituted or corrupted model would still load, still run, "
            "and return confident nonsense, so the fetch refuses it."
        )
    staging.replace(target)
    print(f"  ok {name}: checksum verified")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dest = Path(args[0]) if args else DEFAULT_DEST
    for name, (repo_path, want_sha256, want_size) in WANTED.items():
        _fetch_one(name, repo_path, want_sha256, want_size, dest)
    print(f"embedding model ready in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
