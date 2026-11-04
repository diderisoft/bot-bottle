"""Keep an image's file ownership and modes in a rootless-built guest rootfs.

A rootfs tree is unpacked from a tar stream by the unprivileged host user, so
every path lands owned by that user, and setuid bits and other modes outside
the umask are lost. `mke2fs -d` copies what it finds, which would leave an
agent able to rewrite its own VM's system files (#347).

So the tar headers are recorded while the tree is unpacked, in
`<tree>/.bb-owners`, and after `mke2fs -d` one `debugfs -w` pass gives every
inode the owner and mode the image had. Files the tar didn't hold (the injected
init and dropbear, markers) become root's. The record lives inside the tree, so
it moves with the staging rename and goes with the tree when the cache prunes
it; the pass removes it from the image."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tarfile
import tempfile
import threading
import typing
from pathlib import Path

from ...lib.log import die, warn

OWNERS_FILE = ".bb-owners"
_CHUNK = 1 << 20

# (uid, gid, permission bits incl. setuid/setgid/sticky) by path, "" for the root.
Owners = dict[str, tuple[int, int, int]]


def _key(name: str) -> str:
    """A tar member name as a path relative to the tree's root."""
    name = name.strip("/")
    while name.startswith("./"):
        name = name[2:]
    return "" if name in (".", "") else name


def owners_of(tar: tarfile.TarFile) -> Owners:
    """Every member's owner and mode. A later member for a path wins, as when
    the tar is unpacked."""
    return {_key(m.name): (m.uid, m.gid, m.mode & 0o7777) for m in tar}


def write_owners(tree: Path, owners: Owners) -> None:
    lines = (json.dumps([path, uid, gid, mode]) for path, (uid, gid, mode) in owners.items())
    (tree / OWNERS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_owners(tree: Path) -> Owners | None:
    """The tree's recorded owners, or None for a tree unpacked before they
    were recorded."""
    try:
        text = (tree / OWNERS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    owners: Owners = {}
    for line in text.splitlines():
        if line:
            path, uid, gid, mode = json.loads(line)
            owners[path] = (int(uid), int(gid), int(mode))
    return owners


def has_owners(tree: Path) -> bool:
    return (tree / OWNERS_FILE).is_file()


def unpack(stream: typing.IO[bytes], dest: Path, tar_args: typing.Sequence[str] = ()) -> int:
    """Unpack the tar `stream` into `dest` with GNU tar, recording its owners
    in `dest`. Returns tar's exit status."""
    read_fd, write_fd = os.pipe()
    owners: Owners = {}
    failure: list[BaseException] = []

    def parse() -> None:
        with os.fdopen(read_fd, "rb") as f:
            try:
                with tarfile.open(fileobj=f, mode="r|") as tar:
                    owners.update(owners_of(tar))
            except (tarfile.TarError, OSError) as e:
                failure.append(e)
            # Drain, so the writer never blocks on a full pipe.
            while f.read(_CHUNK):
                pass

    reader = threading.Thread(target=parse, daemon=True)
    reader.start()
    with subprocess.Popen(["tar", "-x", *tar_args, "-C", str(dest)],
                          stdin=subprocess.PIPE) as untar:
        assert untar.stdin is not None
        with os.fdopen(write_fd, "wb") as copy:
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                copy.write(chunk)
                try:
                    untar.stdin.write(chunk)
                except BrokenPipeError:
                    break
        try:
            untar.stdin.close()
        except BrokenPipeError:
            pass
        rc = untar.wait()
    reader.join()
    if rc == 0:
        if failure:
            die(f"reading the rootfs tar's owners failed: {failure[0]}")
        write_owners(dest, owners)
    return rc


def record_from_file(tar_path: Path, dest: Path) -> None:
    """Record the owners of an already-unpacked tar file (any compression)."""
    try:
        with tarfile.open(tar_path, mode="r:*") as tar:
            owners = owners_of(tar)
    except (tarfile.TarError, OSError) as e:
        die(f"reading the owners in {tar_path} failed: {e}")
    write_owners(dest, owners)


def _quoted(path: str) -> str | None:
    """`path` as one debugfs argument, or None when debugfs can't take it."""
    if '"' in path or "\n" in path or "\\" in path:
        return None
    return f'"/{path}"'


def ownership_commands(tree: Path, owners: Owners) -> tuple[list[str], int]:
    """The debugfs commands that give each inode in `tree` its recorded owner
    and mode (root's, for one the tar didn't hold), and how many paths debugfs
    couldn't be told about."""
    commands: list[str] = []
    skipped = 0
    entries: list[str] = [""]
    for dirpath, dirnames, filenames in os.walk(tree):
        rel = os.path.relpath(dirpath, tree)
        prefix = "" if rel == "." else rel + "/"
        entries.extend(prefix + name for name in dirnames + filenames)
    for path in entries:
        if path == OWNERS_FILE:
            continue
        st = os.lstat(tree / path) if path else os.lstat(tree)
        uid, gid, mode = owners.get(path, (0, 0, -1))
        quoted = _quoted(path) if path else '"/"'
        if quoted is None:
            skipped += 1
            continue
        if st.st_uid != uid:
            commands.append(f"sif {quoted} uid {uid}")
        if st.st_gid != gid:
            commands.append(f"sif {quoted} gid {gid}")
        if mode >= 0 and not stat.S_ISLNK(st.st_mode) and stat.S_IMODE(st.st_mode) != mode:
            commands.append(f"sif {quoted} mode 0{stat.S_IFMT(st.st_mode) | mode:o}")
    return commands, skipped


def apply(tree: Path, image: Path) -> None:
    """Give `image` (built by `mke2fs -d tree`) the tree's recorded owners and
    modes, and drop the record from it. A tree with no record is left as is."""
    owners = read_owners(tree)
    if owners is None:
        return
    commands, skipped = ownership_commands(tree, owners)
    commands.append(f"rm /{OWNERS_FILE}")
    with tempfile.NamedTemporaryFile("w", prefix="bb-owners-", suffix=".debugfs",
                                     delete=False, encoding="utf-8") as script:
        script.write("\n".join(commands) + "\n")
    try:
        result = subprocess.run(["debugfs", "-w", "-f", script.name, str(image)],
                                capture_output=True, text=True, check=False)
    finally:
        os.unlink(script.name)
    if result.returncode != 0:
        die(f"setting the rootfs owners in {image} failed: {result.stderr.strip()[-300:]}")
    if skipped:
        warn(f"{skipped} path(s) in {tree} have names debugfs can't take; "
             "they keep the build user's owner")
    _check(image, owners)


def _check(image: Path, owners: Owners) -> None:
    """Fail closed if the root dir didn't get its owner: debugfs reports bad
    commands only on stdout and still exits 0."""
    uid, gid, _ = owners.get("", (0, 0, -1))
    shown = subprocess.run(["debugfs", "-R", "stat /", str(image)],
                           capture_output=True, text=True, check=False).stdout
    if f"User: {uid:>5}" not in shown or f"Group: {gid:>5}" not in shown:
        die(f"setting the rootfs owners in {image} failed: / isn't {uid}:{gid}")


__all__ = ["OWNERS_FILE", "Owners", "apply", "has_owners", "ownership_commands",
           "owners_of", "read_owners", "record_from_file", "unpack", "write_owners"]
