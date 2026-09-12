"""Run with littlefs-python installed; normal firmware tests may skip this tool test."""
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

try:
    import littlefs
except ImportError:
    raise unittest.SkipTest("Storage migration tests require littlefs-python==0.19.0")

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("migration", ROOT / "scripts/migrate-sr-storage.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def backup(self):
        flash = bytearray(b"\xff" * 0x1000000)
        entries = {"nvs": (0x9000, 0x10000), "otadata": (0x19000, 0x2000),
                   "ota_0": (0x20000, 0x400000), "ota_1": (0x420000, 0x400000),
                   "littlefs": (0x820000, 0x700000), "coredump": (0xF20000, 0x10000)}
        for index, (label, (offset, size)) in enumerate(entries.items()):
            struct.pack_into("<HBBII16sI", flash, 0x8000 + 32 * index,
                             0x50AA, 1, 0, offset, size, label.encode(), 0)
        fs = migration.filesystem(b"\xff" * 0x700000, name_max=255)
        fs.format()
        fs.mount()
        fs.makedirs("/wallpapers")
        fs.makedirs("/empty")
        with fs.open("/wallpapers/current.jpg", "wb") as handle:
            handle.write(bytes(range(256)) * 32)
        fs.setattr("/wallpapers/current.jpg", "t", struct.pack("<I", 123456))
        fs.setattr("/empty", 42, b"directory attribute")
        fs.unmount()
        flash[0x820000:0xF20000] = fs.context.buffer
        return bytes(flash)

    def test_files_attributes_and_backup_survive_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "backup.bin"
            data = self.backup()
            backup.write_bytes(data)
            output = Path(directory) / "out"
            migration.prepare(backup, output)
            self.assertEqual(backup.read_bytes(), data)
            image = (output / "littlefs-5m.bin").read_bytes()
            self.assertEqual(len(image), 0x500000)
            fs = migration.filesystem(image)
            fs.mount()
            files, dirs, attrs = migration.snapshot(fs)
            self.assertEqual(files["/wallpapers/current.jpg"], bytes(range(256)) * 32)
            self.assertIn("/empty", dirs)
            self.assertEqual(attrs["/empty"][42], b"directory attribute")
            fs.unmount()
            with self.assertRaises(FileExistsError):
                migration.prepare(backup, output)

    def test_partial_backup_is_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / "backup.bin"
            backup.write_bytes(b"partial")
            output = Path(directory) / "out"
            with self.assertRaises(ValueError):
                migration.prepare(backup, output)
            self.assertFalse(output.exists())

    def test_corrupt_filesystem_is_not_auto_formatted(self):
        with tempfile.TemporaryDirectory() as directory:
            data = bytearray(self.backup())
            data[0x820000:0x822000] = b"\xff" * 8192
            backup = Path(directory) / "backup.bin"
            backup.write_bytes(data)
            output = Path(directory) / "out"
            with self.assertRaises(littlefs.LittleFSError):
                migration.prepare(backup, output)
            self.assertFalse(output.exists())
