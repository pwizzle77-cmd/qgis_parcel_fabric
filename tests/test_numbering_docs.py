import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import documents as D
from core import numbering as N


def test_format_and_parse():
    s = N.format_number(12, 4, "S", 16, 2942, 0)
    assert s == "12-4S-16-02942-000"
    p = N.parse_number(s)
    assert p == {"section": 12, "township": 4, "twp_dir": "S", "range": 16, "pid": 2942, "split": 0}
    assert N.parse_number("12-04S-16E-2942-0")["pid"] == 2942
    assert N.parse_number("not a number") is None


def test_normalize_plss_values():
    assert N.normalize_township("04S") == (4, "S")
    assert N.normalize_township("T4S") == (4, "S")
    assert N.normalize_township(4, "South") == (4, "S")
    assert N.normalize_range("R16E") == 16
    assert N.normalize_section("07") == 7
    with pytest.raises(N.NumberError):
        N.normalize_township("4")
    with pytest.raises(N.NumberError):
        N.normalize_section(37)


def test_next_parcel_id_per_str_includes_retired():
    existing = ["12-4S-16-02942-000", "12-4S-16-02950-001", "13-4S-16-09999-000", None, "junk"]
    assert N.next_parcel_id(existing, 12, 4, "S", 16) == 2951
    assert N.next_parcel_id(existing, 1, 5, "S", 17) == 1


def test_split_numbers_continue_from_highest():
    existing = ["12-4S-16-02942-000", "12-4S-16-02942-001", "12-4S-16-02942-002"]
    assert N.split_numbers(existing, "12-4S-16-02942-000", 3) == [
        "12-4S-16-02942-003", "12-4S-16-02942-004", "12-4S-16-02942-005"]
    assert N.split_numbers([], "12-4S-16-02942-000", 1) == ["12-4S-16-02942-001"]


def test_split_parts_reads_the_suffix():
    assert N.split_parts("12-4S-16-02942-001") == ("12-4S-16-02942", 1)
    assert N.split_parts("02-5S-17-09063-000") == ("02-5S-17-09063", 0)
    assert N.split_parts("12345-002") == ("12345", 2)
    assert N.split_parts("12345") is None
    assert N.split_parts("1234567-1234") is None


def test_fields_for_keeps_a_roll_id():
    roll = N.fields_for("1234567-1234")
    assert roll["ParcelNumber"] == "1234567-1234"
    assert roll["Section"] is None and roll["Township"] is None and roll["Range"] is None
    assert roll["ParcelID"] is None and roll["ParcelSeq"] is None and roll["SplitNo"] is None
    strap = N.fields_for("12-4S-16-02942-000")
    assert strap["ParcelNumber"] == "12-4S-16-02942-000"
    assert strap["Section"] == 12 and strap["Township"] == "4S" and strap["Range"] == 16
    assert strap["ParcelID"] == "02942-000" and strap["ParcelSeq"] == 2942 and strap["SplitNo"] == 0
    with pytest.raises(N.NumberError):
        N.fields_for("x" * 31)


def test_document_paths():
    v = D.record_values("PB 5 PG 22", "Plat", "5", "22", "2024-03-01", "{ABC-1}")
    rel = D.relative_path(D.DEFAULT_FOLDER, D.DEFAULT_NAME, v, ".PDF")
    assert rel == ["Plat", "Book 5", "Plat_B5_P22.pdf"]
    assert D.safe('Deed: OR 12/3?') == "Deed_ OR 12_3_"
    assert D.relative_path("{year}/{type}", "{name}", v, "pdf") == ["2024", "Plat", "PB 5 PG 22.pdf"]
    # blank tokens don't leave empty folders
    assert D.relative_path("{type}/{book}", "{id}", D.record_values(rtype="Deed"), ".tif") == ["Deed", "document.tif"]


def test_store_creates_folders_and_never_overwrites():
    root = tempfile.mkdtemp()
    src = os.path.join(tempfile.mkdtemp(), "scan.pdf")
    with open(src, "w") as f:
        f.write("x")
    v = D.record_values("PB 5 PG 22", "Plat", "5", "22")
    a = D.store(src, root, D.DEFAULT_FOLDER, D.DEFAULT_NAME, v)
    b = D.store(src, root, D.DEFAULT_FOLDER, D.DEFAULT_NAME, v)
    assert a == os.path.join(root, "Plat", "Book 5", "Plat_B5_P22.pdf")
    assert b.endswith("Plat_B5_P22 (2).pdf")
    assert os.path.exists(src)          # copy, not move
    assert D.is_inside(a, root) and not D.is_inside(src, root)
    # storing the file that is already the target is a no-op
    assert D.store(a, root, D.DEFAULT_FOLDER, D.DEFAULT_NAME, v) == a
