import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import lsa as L


def net(dist_ab=200.0, shift=(0.0, 0.0)):
    """A 200 x 100 lot. Points start where the record says, plus ``shift``."""
    true = {"A": (0, 0), "B": (200, 0), "C": (200, 100), "D": (0, 100)}
    pts = {k: L.Point(k, x + shift[0], y + shift[1]) for k, (x, y) in true.items()}
    obs = []
    for key, a, b, d in (("ab", "A", "B", dist_ab), ("bc", "B", "C", 100), ("cd", "C", "D", 200),
                         ("da", "D", "A", 100)):
        ax, ay = true[a]
        bx, by = true[b]
        az = math.atan2(bx - ax, by - ay)
        obs += L.observations_for_line(key, a, b, az, d, 0.02, 50, 10)
    return pts, obs


def test_consistent_network_has_no_residuals():
    pts, obs = net()
    r = L.adjust(pts, obs)
    assert r.converged and r.held == "A"
    assert max(abs(o.residual) for o in r.obs) < 1e-6
    assert r.points["C"].x == pytest.approx(200) and r.points["C"].y == pytest.approx(100)


def test_blunder_stands_out_in_consistency_check():
    pts, obs = net(dist_ab=201.0)        # a 1 ft typo on the south line
    r = L.adjust(pts, obs)
    worst = r.worst(2)
    assert {w.obs.key for w in worst} >= {"ab"} or {w.obs.key for w in worst} >= {"cd"}
    assert abs(worst[0].normalized) > 3
    assert r.sigma0 > 3
    assert r.chi_square_ok() is False


def test_control_moves_the_network():
    pts, obs = net(shift=(0.3, -0.2))     # coordinates off, record good
    pts["A"] = L.Point("A", 0.0, 0.0, L.FIXED)
    pts["C"] = L.Point("C", 200.0, 100.0, L.FIXED)
    r = L.adjust(pts, obs)
    assert r.converged
    assert r.points["B"].x == pytest.approx(200, abs=1e-6)
    assert r.points["B"].dx == pytest.approx(-0.3, abs=1e-6)
    assert r.points["D"].y == pytest.approx(100, abs=1e-6)
    assert r.points["B"].a >= r.points["B"].b >= 0


def test_weighted_control_is_honoured():
    pts, obs = net()
    pts["A"] = L.Point("A", 0.0, 0.0, L.WEIGHTED, sd=0.01)
    pts["C"] = L.Point("C", 200.05, 100.0, L.WEIGHTED, sd=0.01)   # control disagrees a little
    r = L.adjust(pts, obs)
    assert r.converged and r.dof > 0
    assert 200.0 < r.points["C"].x < 200.05


def test_unsolvable_network_is_reported():
    pts = {"A": L.Point("A", 0, 0, L.FIXED), "B": L.Point("B", 10, 0)}
    obs = [L.Obs("x", "A", "B", "dist", 10, 0.01)]        # no direction: B can't be placed
    r = L.adjust(pts, obs)
    assert r.warnings


def test_chi2_bounds_reasonable():
    lo, hi = L.chi2_bounds(10)
    assert 3.0 < lo < 3.4 and 20.0 < hi < 20.8


def test_ellipse_points():
    pts = L.ellipse_points(0, 0, 2, 1, 0)
    assert pts[0] == pytest.approx((0, 2))     # major axis points north
