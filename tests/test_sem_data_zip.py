import contextlib
import io
import json
import pathlib
import stat
import tempfile
import unittest
import zipfile

from sem_data import cli
from sem_data.safezip import EXTRACTION_RECORD, UnsafeArchiveError, extract_archive, safe_member_parts


def _zip(path, entries):
    """entries: list of (name, bytes) or (ZipInfo, bytes)."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return path


class SafeMemberPartsTest(unittest.TestCase):
    def test_rejects_unsafe_names(self):
        for name in ("../x", "a/../../x", "/etc/x", "\\x", "C:/x", "C:x", "a/b:stream",
                     "a\\..\\..\\x", "", "nul.txt", "a /b", "a./b", "a\x01b"):
            with self.subTest(name=name), self.assertRaises(UnsafeArchiveError):
                safe_member_parts(name)

    def test_accepts_normal_names(self):
        self.assertEqual(safe_member_parts("data/images/a.jpg"), ("data", "images", "a.jpg"))
        self.assertEqual(safe_member_parts("data\\masks\\a.png"), ("data", "masks", "a.png"))
        self.assertEqual(safe_member_parts("./data/x"), ("data", "x"))


class ExtractArchiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.root = self.tmp / "data"

    def tearDown(self):
        self._tmp.cleanup()

    def assertRejected(self, archive, **limits):
        with self.assertRaises(UnsafeArchiveError):
            extract_archive(archive, self.root, **limits)
        self.assertFalse(self.root.exists(), "nothing may be written on rejection")
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.is_dir()], [])
        self.assertFalse((self.tmp / "evil").exists())

    def test_valid_archive_extracts_with_record(self):
        archive = _zip(self.tmp / "ok.zip", [("data/images/a.jpg", b"img"), ("data/carinthia-s.csv", b"x")])
        record = extract_archive(archive, self.root)
        self.assertEqual((self.root / "data/images/a.jpg").read_bytes(), b"img")
        self.assertEqual(record["files"], 2)
        stored = json.loads((self.root / EXTRACTION_RECORD).read_text(encoding="utf-8"))
        self.assertEqual(stored["archive_sha256"], record["archive_sha256"])
        self.assertFalse((self.tmp / ".data.extracting").exists())

    def test_traversal_absolute_and_drive_entries(self):
        for index, name in enumerate(("../evil", "/evil", "C:/evil", "D:evil", "a/../../evil")):
            with self.subTest(name=name):
                self.assertRejected(_zip(self.tmp / f"bad{index}.zip", [("ok.txt", b"1"), (name, b"x")]))

    def test_symlink_entry(self):
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assertRejected(_zip(self.tmp / "link.zip", [(info, b"/etc/passwd")]))

    def test_case_and_duplicate_collisions(self):
        self.assertRejected(_zip(self.tmp / "case.zip", [("a.txt", b"1"), ("A.TXT", b"2")]))
        with self.assertWarns(UserWarning):
            archive = _zip(self.tmp / "dup.zip", [("a.txt", b"1"), ("a.txt", b"2")])
        self.assertRejected(archive)
        self.assertRejected(_zip(self.tmp / "filedir.zip", [("a", b"1"), ("a/b", b"2")]))

    def test_size_limits(self):
        archive = _zip(self.tmp / "big.zip", [("a", b"x" * 100), ("b", b"y" * 100)])
        self.assertRejected(archive, max_file_bytes=99)
        self.assertRejected(archive, max_total_bytes=150)
        self.assertRejected(archive, max_entries=1)

    def test_reserved_record_name(self):
        self.assertRejected(_zip(self.tmp / "rec.zip", [(EXTRACTION_RECORD, b"{}")]))

    def test_refuses_nonempty_root(self):
        self.root.mkdir()
        (self.root / "keep").write_text("x")
        archive = _zip(self.tmp / "ok.zip", [("a", b"1")])
        with self.assertRaises(FileExistsError):
            extract_archive(archive, self.root)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["keep"])

    def test_cli_reports_failure_as_json(self):
        archive = _zip(self.tmp / "bad.zip", [("../evil", b"x")])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["extract", "--archive", str(archive), "--root", str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "failed")
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
