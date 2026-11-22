"""tests/integration/t44a_qualification/restore_files.py

Cold, numeric-owner mount-closure inventory; invoked only on the owned CI tree.
"""

import hashlib
import json
import os
import posixpath
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOTS = ("pgdata", "source", "tools", "scripts", "work", "home", "fixture", "secrets")
REPORT_BOUND = 160 * 1024**2


def digest(data):
    """Hash original bytes without emitting values."""
    return hashlib.sha256(data).hexdigest()


def linked_target(name, target):
    """Resolve only included container paths, never host or outside-tree links."""
    if target.startswith("/"):
        mapping = {"/home/probe": "home", "/var/lib/postgresql/data": "pgdata"}
        for source in sorted(mapping, key=len, reverse=True):
            if target == source or target.startswith(source + "/"):
                target = mapping[source] + target[len(source) :]
                break
        else:
            target = target.lstrip("/")
    else:
        target = posixpath.join(posixpath.dirname(name), target)
    target = posixpath.normpath(target)
    assert target.split("/")[0] in ROOTS and ".." not in PurePosixPath(target).parts, (
        "External restore link"
    )
    return target


def resolved_member(name, rows):
    """Resolve actual container paths; only mount-parent prefixes need no inventory row."""
    mounts = {"/" + member: member for member in ROOTS if member not in {"home", "pgdata"}}
    mounts.update({"/home/probe": "home", "/var/lib/postgresql/data": "pgdata"})
    prefixes = {str(parent) for mount in mounts for parent in PurePosixPath(mount).parents}

    def member_at(path):
        """Map exact container mounts without treating compact inventory names as aliases."""
        return next(
            (
                member + path[len(mount) :]
                for mount, member in mounts.items()
                if path == mount or path.startswith(mount + "/")
            ),
            None,
        )

    mount = next(path for path, member in mounts.items() if member == name.split("/")[0])
    pending = (mount + name[len(name.split("/")[0]) :]).split("/")
    current = []
    followed = 0
    while pending:
        part = pending.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            current = current[:-1]
            continue
        path = "/" + "/".join([*current, part])
        entry = member_at(path)
        if entry is None:
            assert path in prefixes, "Outside container mounts: " + name + " -> " + path
            current.append(part)
            continue
        assert entry in rows, "Link member absent from backup closure: " + name + " -> " + entry
        row = rows[entry]
        if row["kind"] == "link":
            followed += 1
            assert followed <= 40, "Cyclic or excessive restore link chain: " + name
            target = row["target"]
            assert target and "\x00" not in target, "Invalid restore link target: " + entry
            if target.startswith("/"):
                current = []
            pending = target.split("/") + pending
        else:
            assert row["kind"] in {"directory", "file"}, "Unsafe restore link member: " + entry
            current.append(part)
            assert not pending or row["kind"] == "directory", (
                "Non-directory restore link parent: " + entry
            )
    resolved = member_at("/" + "/".join(current))
    assert resolved in rows, "Restore link ends outside mounted closure: " + name
    return resolved


def inventory(root):
    """Read every cold file and its ownership, preserving links without following them."""
    rows = {}
    archive_upper = 4 * 1024**2
    restore_upper = 0
    for member in ROOTS:
        top = root / member
        assert top.is_dir() and not top.is_symlink(), "Missing mount root"
        for path in [top, *sorted(top.rglob("*"))]:
            st = path.lstat()
            name = path.relative_to(root).as_posix()
            row = {
                "mode": stat.S_IMODE(st.st_mode),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "device": st.st_dev,
                "inode": st.st_ino,
            }
            if stat.S_ISDIR(st.st_mode):
                row["kind"] = "directory"
            elif stat.S_ISREG(st.st_mode):
                with path.open("rb") as stream:
                    hashed = hashlib.file_digest(stream, "sha256").hexdigest()
                row.update(kind="file", bytes=st.st_size, sha256=hashed)
                archive_upper += ((st.st_size + 511) // 512) * 512
                restore_upper += ((max(st.st_size, st.st_blocks * 512) + 4095) // 4096) * 4096
            else:
                assert stat.S_ISLNK(st.st_mode) and member != "pgdata", "Unsafe cold entry"
                target = os.readlink(path)
                row.update(kind="link", target=target)
            rows[name] = row
            archive_upper += (
                8192
                + ((len(name.encode()) + len(row.get("target", "").encode()) + 1023) // 512) * 512
            )
            restore_upper += 4096
    for name, row in rows.items():
        if row["kind"] == "link":
            row["resolved"] = resolved_member(name, rows)
    key_path = root / "secrets/app.json"
    keys = json.loads(key_path.read_text())
    assert keys and all(isinstance(v, str) and len(v) >= 16 for v in keys.values())
    secrets_rows = {
        k: {"bytes": len(v.encode()), "sha256": digest(v.encode())} for k, v in keys.items()
    }
    assert all(
        row["mode"] == 0o600
        for name, row in rows.items()
        if name.startswith("secrets/") and name != "secrets/readiness" and row["kind"] == "file"
    )
    return {
        "rows": rows,
        "key_hashes": secrets_rows,
        "archive_upper": archive_upper,
        "restore_upper": restore_upper,
    }


def compare(original, replacement):
    """Require complete byte/mode/key equality and independently allocated regular files."""
    assert original["key_hashes"] == replacement["key_hashes"], "Restored key identity"
    assert original["rows"].keys() == replacement["rows"].keys(), "Incomplete restored closure"
    old_inodes = {
        (r["device"], r["inode"]) for r in original["rows"].values() if r["kind"] == "file"
    }
    for name, row in replacement["rows"].items():
        expected = original["rows"][name]
        assert {k: v for k, v in row.items() if k not in {"device", "inode"}} == {
            k: v for k, v in expected.items() if k not in {"device", "inode"}
        }, "Restore bytes/rights mismatch: " + name
        if row["kind"] == "file":
            assert (row["device"], row["inode"]) not in old_inodes, "Original inode fallback"


def audit_archive(archive, original):
    """Fail before extraction on unbound names, links, ownership or entry types."""
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        assert len(members) == len(original["rows"]), "Archive member count"
        seen = set()
        for member in members:
            name = member.name.rstrip("/")
            assert name in original["rows"] and name not in seen, "Unexpected archive member"
            assert not name.startswith("/") and ".." not in PurePosixPath(name).parts
            seen.add(name)
            row = original["rows"][name]
            assert (member.mode, member.uid, member.gid) == (row["mode"], row["uid"], row["gid"])
            if member.islnk():
                assert row["kind"] == "file" and member.linkname in original["rows"]
                peer = original["rows"][member.linkname]
                assert peer["kind"] == "file" and (peer["device"], peer["inode"]) == (
                    row["device"],
                    row["inode"],
                )
            elif member.isfile():
                assert row["kind"] == "file" and member.size == row["bytes"]
                with stream.extractfile(member) as content:
                    assert hashlib.file_digest(content, "sha256").hexdigest() == row["sha256"]
            elif member.issym():
                assert row["kind"] == "link" and member.linkname == row["target"]
            else:
                assert member.isdir() and row["kind"] == "directory"


def main():
    """Write private hash-only inventory; print only its bounded identity/fit summary."""
    verify = sys.argv[-1] == "--verify"
    root, source, output = map(Path, sys.argv[1:4])
    assert root.name == "t44a03" and root.resolve(strict=True) == root
    assert source in {root, root / "restore-replacement"} and source.resolve(strict=True) == source
    assert output.parent == root / "restore-backup" and output.exists() == verify
    report = inventory(source)
    body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    assert len(body) <= REPORT_BOUND, (
        f"Private inventory bound: bytes={len(body)} limit={REPORT_BOUND} "
        f"entries={len(report['rows'])}"
    )
    if verify:
        assert output.read_bytes() == body, "Original cold tree changed during archive"
    else:
        output.write_bytes(body)
        output.chmod(0o600)
        os.chown(output, root.stat().st_uid, root.stat().st_gid)
    file_rows = [row for row in report["rows"].values() if row["kind"] == "file"]
    groups = {(row["device"], row["inode"]): row["bytes"] for row in file_rows}
    print(
        json.dumps(
            {
                "file_entries": len(file_rows),
                "file_inode_groups": len(groups),
                "file_hardlink_aliases": len(file_rows) - len(groups),
                "unique_file_logical_bytes": sum(groups.values()),
                "all_file_logical_bytes": sum(row["bytes"] for row in file_rows),
                "sha256": digest(body),
                "bytes": len(body),
                "entries": len(report["rows"]),
                "archive_upper": report["archive_upper"],
                "restore_upper": report["restore_upper"],
            }
        )
    )


if __name__ == "__main__":
    main()
