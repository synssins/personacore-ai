"""Container entrypoint: take ownership of appdata, then drop privileges.

Spec section 7 requires the process to run as a non-root user, and it does — the
server never runs as root. But a bind-mounted host directory arrives owned by
whoever created it, usually root, and a process that is already unprivileged
cannot chown its way in. That produced a container which built, started, and then
died on "Permission denied: /appdata/plugins".

So: start as root, fix ownership of the appdata mount only, drop to the target
uid/gid, and exec the server. The privileged window is a few milliseconds of
chown with no network listener open, and nothing after that point is root.

PUID/PGID are honoured because every other service on the target host uses them;
an assistant that needs a different ownership convention from everything beside
it is one more thing to remember at 2am. Default 10001, the uid the image
creates.

If the container is already running as a non-root user (`user:` in Compose, or a
platform that forbids root), no chown is attempted and the server is exec'd
directly — an operator who has taken ownership of the problem is not overruled.
"""

from __future__ import annotations

import grp
import os
import pwd
import sys
from pathlib import Path

APPDATA = Path(os.environ.get("PERSONACORE_APPDATA", "/appdata"))


def _target_ids() -> tuple[int, int]:
    try:
        uid = int(os.environ.get("PUID", "10001"))
        gid = int(os.environ.get("PGID", "10001"))
    except ValueError:
        print("PUID and PGID must be numeric; falling back to 10001", file=sys.stderr)
        return 10001, 10001
    return uid, gid


def _own(path: Path, uid: int, gid: int) -> int:
    """Chown the tree, counting what changed. Returns the number of paths fixed.

    Only walks appdata. It deliberately does not touch anything else on the
    filesystem, and it skips paths whose ownership is already correct so a large
    volume is not rewritten on every start.
    """
    fixed = 0
    targets = [path, *path.rglob("*")] if path.exists() else [path]
    for entry in targets:
        try:
            stat = entry.lstat()
            if stat.st_uid == uid and stat.st_gid == gid:
                continue
            os.lchown(entry, uid, gid)
            fixed += 1
        except OSError as exc:
            # One unreadable entry must not stop the container starting. The
            # server reports a genuinely unusable appdata far more clearly than
            # a traceback from here would.
            print(f"could not take ownership of {entry}: {exc}", file=sys.stderr)
    return fixed


def main(argv: list[str]) -> int:
    command = argv[1:] or [
        "python",
        "-m",
        "personacore",
        "serve",
        "--host",
        os.environ.get("PERSONACORE_HOST", "0.0.0.0"),  # noqa: S104
        "--port",
        os.environ.get("PERSONACORE_PORT", "8053"),
    ]

    if os.geteuid() != 0:
        os.execvp(command[0], command)

    uid, gid = _target_ids()
    APPDATA.mkdir(parents=True, exist_ok=True)
    fixed = _own(APPDATA, uid, gid)
    if fixed:
        print(f"took ownership of {fixed} path(s) under {APPDATA} for {uid}:{gid}")

    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    try:
        grp.getgrgid(gid)
    except KeyError:
        pass

    # Drop the supplementary groups before the gid, and the gid before the uid.
    # Any other order leaves privilege behind: setuid first and the setgid call
    # that follows will fail, silently keeping root's group.
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() != uid:
        print("failed to drop privileges; refusing to run as root", file=sys.stderr)
        return 1

    os.environ.setdefault("HOME", "/tmp")  # noqa: S108 - the runtime user has no home
    os.environ["USER"] = name
    os.execvp(command[0], command)
    return 0  # unreachable; execvp replaces the process


if __name__ == "__main__":
    sys.exit(main(sys.argv))
