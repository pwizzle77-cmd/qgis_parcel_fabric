import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

from core.topo import chain_ring, dangles


def test_chain_ring_mixed_directions():
    segs = [("a", (0, 0), (0, 1)), ("c", (1, 0), (1, 1)),   # c stored upward
            ("b", (0, 1), (1, 1)), ("d", (0, 0), (1, 0))]   # d stored eastward
    ring = chain_ring(segs, 1e-6)
    assert ring == [("a", False), ("b", False), ("c", True), ("d", True)]


def test_chain_ring_open_returns_none():
    segs = [("a", (0, 0), (0, 1)), ("b", (0, 1), (1, 1))]
    assert chain_ring(segs, 1e-6) is None


def test_chain_ring_tolerance():
    segs = [("a", (0, 0), (0, 1)), ("b", (0.004, 1), (1, 1)),
            ("c", (1, 1), (1, 0)), ("d", (1, 0), (0.003, 0))]
    assert chain_ring(segs, 0.01) is not None
    assert chain_ring(segs, 0.001) is None


def test_dangles():
    ends = [("a", (0, 0)), ("a", (0, 1)), ("b", (0, 1)), ("b", (1, 1))]
    d = dangles(ends, 0.01)
    assert sorted(x[1] for x in d) == [(0, 0), (1, 1)]
