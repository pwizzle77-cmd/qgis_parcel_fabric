# -*- coding: utf-8 -*-
"""Parcel numbers: Section-Township-Range-ParcelID-Split.

Example: ``12-4S-16-02942-000``
  * section 2 digits, township number + N/S, range 2 digits (no E/W),
  * 5-digit ParcelID, unique within its section-township-range,
  * 3-digit split: 000 for an original parcel, 001, 002... for splits.

Pure Python (no QGIS imports).
"""

import re

NUMBER_RE = re.compile(r"^\s*(\d{1,2})-(\d{1,2})([NS])-(\d{1,2})[EW]?-(\d{1,5})-(\d{1,3})\s*$", re.I)


class NumberError(ValueError):
    pass


def _int(v, label):
    s = str(v).strip() if v is not None else ""
    m = re.search(r"\d+", s)
    if not m:
        raise NumberError("%s has no number (%r)" % (label, v))
    return int(m.group(0))


def normalize_section(v):
    n = _int(v, "Section")
    if not 1 <= n <= 36:
        raise NumberError("Section must be 1-36, got %r" % v)
    return n


def normalize_township(v, direction=None):
    """'04S', '4S', 'T4S', '4 S', 4 (+ 'S') -> (4, 'S')."""
    s = str(v).strip().upper() if v is not None else ""
    n = _int(s, "Township")
    d = (direction or "").strip().upper()[:1]
    if not d:
        letters = re.findall(r"[NS]", s.replace("T", "", 1) if s.startswith("T") else s)
        d = letters[-1] if letters else ""
    if d not in ("N", "S"):
        raise NumberError("Township %r has no N/S direction - set a direction field" % v)
    return n, d


def normalize_range(v):
    """'16', '16E', 'R16E' -> 16."""
    return _int(v, "Range")


def format_number(section, twp, twp_dir, rng, pid, split=0):
    return "%02d-%d%s-%02d-%05d-%03d" % (section, twp, twp_dir, rng, pid, split)


def parse_number(s):
    """'12-4S-16-02942-000' -> dict, or None if it isn't one."""
    if not s:
        return None
    m = NUMBER_RE.match(str(s))
    if not m:
        return None
    sec, twp, tdir, rng, pid, split = m.groups()
    return {"section": int(sec), "township": int(twp), "twp_dir": tdir.upper(),
            "range": int(rng), "pid": int(pid), "split": int(split)}


def fields_for(number):
    """ParcelNumber and its strap parts. A roll ID that is not a strap is stored as typed.

    Section, township, range and the strap pieces are cleared so a county ID is not
    half a section number. ParcelNumber holds 30 characters, which covers a 26-character roll ID.
    """
    text = "" if number is None else str(number).strip()
    if len(text) > 30:
        raise NumberError("A parcel number can be at most 30 characters.")
    p = parse_number(text)
    if p is None:
        return {"ParcelNumber": text or None, "Section": None, "Township": None,
                "Range": None, "ParcelID": None, "ParcelSeq": None, "SplitNo": None}
    return {"ParcelNumber": text, "Section": p["section"],
            "Township": "%d%s" % (p["township"], p["twp_dir"]), "Range": p["range"],
            "ParcelID": "%05d-%03d" % (p["pid"], p["split"]), "ParcelSeq": p["pid"],
            "SplitNo": p["split"]}


def number_pattern(settings):
    """(separator, digits, parent suffix). Missing values are -, 3, 000."""
    s = settings or {}
    sep = str(s.get("number_separator") or "-")
    try:
        digits = int(str(s.get("number_split_digits") or "3").strip())
    except ValueError:
        digits = 3
    if digits < 1 or digits > 6:
        digits = 3
    parent = s.get("number_parent_suffix")
    parent = "000" if parent is None or str(parent).strip() == "" else str(parent).strip()
    return sep, digits, parent


def clean_pattern(separator, digits, parent):
    """The three parcel-number boxes. Digits must be 1 to 6."""
    sep = str(separator or "").strip() or "-"
    try:
        width = int(str(digits).strip())
    except (TypeError, ValueError):
        raise NumberError("Split digits must be a whole number from 1 to 6.")
    if not 1 <= width <= 6:
        raise NumberError("Split digits must be a whole number from 1 to 6.")
    suffix = str(parent or "").strip() or "000"
    return sep, width, suffix


def split_parts(number, separator="-", digits=3):
    """(stem, split) when the number ends in a split suffix, else None.

    Defaults: '12345-002' -> ('12345', 2). A four-digit tail is not a split, so
    1234567-1234 stays one number until digits is 4. A hyphen still reads a
    Florida strap first: '12-4S-16-02942-001' -> ('12-4S-16-02942', 1).
    Another separator does not.
    """
    text = "" if number is None else str(number).strip()
    if not text:
        return None
    sep = separator if separator else "-"
    try:
        width = int(digits)
    except (TypeError, ValueError):
        width = 3
    width = 1 if width < 1 else 6 if width > 6 else width
    if sep == "-":
        parsed = parse_number(text)
        if parsed:
            stem = format_number(parsed["section"], parsed["township"], parsed["twp_dir"],
                                 parsed["range"], parsed["pid"], 0).rsplit("-", 1)[0]
            return stem, parsed["split"]
    match = re.match(r"^(.*)%s(\d{1,%d})$" % (re.escape(sep), width), text)
    if not match or not match.group(1):
        return None
    return match.group(1), int(match.group(2))


def str_key(section, twp, twp_dir, rng):
    return (int(section), int(twp), str(twp_dir).upper(), int(rng))


def next_parcel_id(existing, section, twp, twp_dir, rng):
    """Next unused ParcelID in a section-township-range.

    ``existing`` is any iterable of parcel number strings (current AND
    retired - a retired number is never reused).
    """
    key = str_key(section, twp, twp_dir, rng)
    used = 0
    for s in existing:
        p = parse_number(s)
        if p and str_key(p["section"], p["township"], p["twp_dir"], p["range"]) == key:
            used = max(used, p["pid"])
    if used >= 99999:
        raise NumberError("No ParcelIDs left in section %d, township %d%s, range %d" % key)
    return used + 1


def next_split(existing, parent_number):
    """Next split suffix under a parent's section-township-range-ParcelID."""
    parent = parse_number(parent_number)
    if parent is None:
        raise NumberError("%r is not a parcel number like 12-4S-16-02942-000" % parent_number)
    base = (parent["section"], parent["township"], parent["twp_dir"], parent["range"], parent["pid"])
    used = 0
    for s in existing:
        p = parse_number(s)
        if p and (p["section"], p["township"], p["twp_dir"], p["range"], p["pid"]) == base:
            used = max(used, p["split"])
    if used >= 999:
        raise NumberError("No split numbers left under %s" % parent_number)
    return parent, used + 1


def split_numbers(existing, parent_number, count):
    """``count`` new split numbers under a parent, in order."""
    parent, first = next_split(existing, parent_number)
    if first + count - 1 > 999:
        raise NumberError("Not enough split numbers left under %s" % parent_number)
    return [format_number(parent["section"], parent["township"], parent["twp_dir"],
                          parent["range"], parent["pid"], first + i) for i in range(count)]
