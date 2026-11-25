import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

from core import documents as D


def test_store_records_matching_sha256(tmp_path):
    src = tmp_path / "deed.pdf"
    src.write_bytes(b"hello deed contents")
    expected = hashlib.sha256(b"hello deed contents").hexdigest()

    root = tmp_path / "store"
    target, digest = D.store(str(src), str(root), D.DEFAULT_FOLDER, D.DEFAULT_NAME,
                             D.record_values(rtype="Plat", book="5", page="22"))
    assert digest == expected
    assert os.path.isfile(target)


def test_verify_true_for_untouched_file(tmp_path):
    src = tmp_path / "deed.pdf"
    src.write_bytes(b"some bytes")
    target, digest = D.store(str(src), str(tmp_path / "store"), D.DEFAULT_FOLDER, D.DEFAULT_NAME,
                             D.record_values(rtype="Deed"))
    assert D.verify(target, digest) is True


def test_verify_false_when_file_changes_after_filing(tmp_path):
    src = tmp_path / "deed.pdf"
    src.write_bytes(b"original bytes")
    target, digest = D.store(str(src), str(tmp_path / "store"), D.DEFAULT_FOLDER, D.DEFAULT_NAME,
                             D.record_values(rtype="Deed"))
    with open(target, "ab") as fh:
        fh.write(b"tampered")
    assert D.verify(target, digest) is False


def test_verify_false_with_no_recorded_hash():
    assert D.verify("/any/path.pdf", "") is False
    assert D.verify("/any/path.pdf", None) is False


def test_sha256_file_missing_returns_none(tmp_path):
    assert D.sha256_file(str(tmp_path / "missing.pdf")) is None
