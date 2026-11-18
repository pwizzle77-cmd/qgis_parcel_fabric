import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

from core import family as F

# A was split into B and C (rec1); C split into D and E (rec2); D and X merged into M (rec3)
EDGES = [("A", "B", "Split", "r1"), ("A", "C", "Split", "r1"),
         ("C", "D", "Split", "r2"), ("C", "E", "Split", "r2"),
         ("D", "M", "Merge", "r3"), ("X", "M", "Merge", "r3")]


def test_roots():
    assert F.roots(EDGES, "E") == {"A"}
    assert F.roots(EDGES, "M") == {"A", "X"}
    assert F.roots(EDGES, "Z") == {"Z"}


def test_family_and_generations():
    fam = F.descendants(EDGES, F.roots(EDGES, "E"))
    assert fam == {"A", "B", "C", "D", "E", "M"}
    gen = F.generations(EDGES, fam | {"X"})
    assert gen["A"] == 0 and gen["C"] == 1 and gen["D"] == 2 and gen["M"] == 3 and gen["X"] == 0


def test_events_sorted_by_date_undated_last():
    dates = {"r1": "1998-04-02", "r2": "2010-07-15"}
    ev = F.events(EDGES, {"A", "B", "C", "D", "E", "M", "X"}, dates, {"r3": "Combination"})
    assert [e["record"] for e in ev] == ["r1", "r2", "r3"]
    assert ev[0]["parents"] == ["A"] and ev[0]["children"] == ["B", "C"]
    assert ev[2]["date"] == "" and set(ev[2]["parents"]) == {"D", "X"}
