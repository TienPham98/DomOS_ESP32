"""Offline, non-destructive preparation of ES3C28P storage for ESP-SR.

Requires littlefs-python==0.19.0. Never connects to or flashes a device.
Input: a complete 16 MiB esptool backup from the old 7 MiB LittleFS layout.
Output: a verified 5 MiB LittleFS image, relocated coredump, and hash manifest.
Keep the backup and outputs private: they can contain credentials/user data.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from littlefs import LittleFS, LittleFSError, UserContext

BLOCK = 4096
OLD_FS = (0x820000, 0x700000)
NEW_FS_SIZE = 0x500000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def partitions(flash):
    result = {}
    for pos in range(0x8000, 0x9000, 32):
        if flash[pos:pos + 2] != b"\xaa\x50":
            break
        _, kind, subtype, offset, size, label, flags = struct.unpack_from("<HBBII16sI", flash, pos)
        if flags:
            raise ValueError("Encrypted/flagged partitions require a separate migration")
        result[label.split(b"\0")[0].decode("ascii")] = (offset, size)
    return result


def filesystem(image, name_max=64):
    # Disable constructor auto-format: corrupt backups must fail, never become empty.
    return LittleFS(context=UserContext(buffer=bytearray(image)), mount=False,
                    block_size=BLOCK, block_count=len(image) // BLOCK, read_size=128,
                    prog_size=128, cache_size=512, lookahead_size=128,
                    block_cycles=512, name_max=name_max)


def snapshot(fs):
    files, dirs, attrs = {}, {"/"}, {}
    for root, directories, names in fs.walk("/"):
        for name in directories:
            dirs.add(root.rstrip("/") + "/" + name)
        for name in names:
            path = root.rstrip("/") + "/" + name
            with fs.open(path, "rb") as handle:
                files[path] = handle.read()
    # Include all LittleFS user attributes, not just Espressif's mtime attribute.
    for path in sorted(dirs | files.keys()):
        attrs[path] = {}
        for code in range(256):
            try:
                attrs[path][code] = fs.getattr(path, code)
            except LittleFSError as exc:
                if exc.code != LittleFSError.Error.LFS_ERR_NOATTR:
                    raise
    return files, dirs, attrs


def prepare(backup, output):
    flash = backup.read_bytes()
    if len(flash) != 0x1000000:
        raise ValueError("Expected complete 16 MiB backup; refusing partial input")
    expected = {"nvs": (0x9000, 0x10000), "otadata": (0x19000, 0x2000),
                "ota_0": (0x20000, 0x400000), "ota_1": (0x420000, 0x400000),
                "littlefs": OLD_FS, "coredump": (0xF20000, 0x10000)}
    actual = partitions(flash)
    if any(actual.get(name) != value for name, value in expected.items()):
        raise ValueError("Backup partition layout is not the expected pre-ESP-SR layout")
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing migration directory")
    # Existing images may advertise the LittleFS default name limit (255)
    # even when ESP-IDF restricts newly created names to 64 characters.
    old = filesystem(flash[OLD_FS[0]:sum(OLD_FS)], name_max=255)
    old.mount()
    content = snapshot(old)
    old.unmount()
    files, dirs, attrs = content
    new = filesystem(b"\xff" * NEW_FS_SIZE)
    new.format()
    new.mount()
    for path in sorted(dirs, key=lambda path: (path.count("/"), path)):
        new.makedirs(path, exist_ok=True)
    for path, data in files.items():
        with new.open(path, "wb") as handle:
            handle.write(data)
    for path, values in attrs.items():
        for code, value in values.items():
            new.setattr(path, code, value)
    new.unmount()
    image = bytes(new.context.buffer)
    verified = filesystem(image)
    verified.mount()
    if snapshot(verified) != content:
        raise ValueError("Migration lost file content, directories or attributes")
    verified.unmount()
    manifest = {"source_sha256": digest(flash), "littlefs_sha256": digest(image),
                "files": {path: {"bytes": len(data), "sha256": digest(data)}
                          for path, data in files.items()},
                "directories": sorted(dirs), "attributes_verified": True,
                "littlefs_offset": "0x820000", "littlefs_size": NEW_FS_SIZE,
                "coredump_offset": "0xff0000",
                "unchanged_partitions": ["nvs", "otadata", "phy_init", "ota_1"]}
    # No output is written until a remount verifies the entire migration.
    output.mkdir(parents=True)
    for name, data in (("littlefs-5m.bin", image),
                       ("coredump-preserved.bin", flash[0xF20000:0xF30000]),
                       ("manifest.json", json.dumps(manifest, indent=2).encode())):
        with (output / name).open("xb") as handle:
            handle.write(data)
    print(json.dumps({"verified": True, "files": len(files),
                      "file_bytes": sum(map(len, files.values())),
                      "littlefs_bytes": len(image), "source_sha256": digest(flash)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.backup.resolve(), args.output.resolve())
