# -*- coding: utf-8 -*-
"""2D least-squares adjustment of a COGO network (needs numpy; QGIS has it).

Observations come from fabric lines: a distance and a direction between the
points at each end (curves use their chord).  Points can be free, fixed, or
weighted (held to their coordinates within a standard deviation).

* Consistency check: no control - one point is held, everything else floats.
  Shows which record dimensions disagree with the rest.
* Weighted adjustment: control points fixed or weighted; the result can be
  applied to the fabric.

Pure Python + numpy (no QGIS), so it can be tested on its own.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

FREE, FIXED, WEIGHTED = 0, 1, 2
SEC = math.pi / (180 * 3600)


@dataclass
class Point:
    key: object
    x: float
    y: float
    mode: int = FREE
    sd: float = 0.05          # for WEIGHTED points (map units)


@dataclass
class Obs:
    key: object               # e.g. the line's feature id
    a: object                 # point key at the start
    b: object                 # point key at the end
    kind: str                 # "dist" or "dir"
    value: float              # map units, or radians (azimuth)
    sd: float                 # map units, or radians


@dataclass
class ObsResult:
    obs: Obs
    computed: float = 0.0
    residual: float = 0.0     # observed - adjusted (map units or radians)
    normalized: float = 0.0   # residual / its standard deviation
    redundancy: float = 0.0


@dataclass
class PointResult:
    key: object
    x: float
    y: float
    dx: float = 0.0
    dy: float = 0.0
    a: float = 0.0            # error ellipse semi-major (1 sigma, map units)
    b: float = 0.0            # semi-minor
    theta: float = 0.0        # azimuth of the major axis (radians)


@dataclass
class Result:
    points: Dict[object, PointResult] = field(default_factory=dict)
    obs: List[ObsResult] = field(default_factory=list)
    iterations: int = 0
    dof: int = 0
    sigma0: float = 1.0       # a-posteriori standard error of unit weight
    converged: bool = False
    held: Optional[object] = None    # point held in a consistency check
    warnings: List[str] = field(default_factory=list)

    def worst(self, n=10):
        return sorted(self.obs, key=lambda r: -abs(r.normalized))[:n]

    def chi_square_ok(self, alpha=0.05):
        """Is sigma0^2 inside the chi-square bounds (two-sided)?"""
        if self.dof <= 0:
            return None
        lo, hi = chi2_bounds(self.dof, alpha)
        return lo <= self.sigma0 ** 2 * self.dof <= hi


def chi2_bounds(dof, alpha=0.05):
    """Wilson-Hilferty approximation of the chi-square bounds (no scipy)."""
    z = {0.05: 1.959964, 0.01: 2.575829}.get(alpha, 1.959964)

    def q(zz):
        k = 2.0 / (9.0 * dof)
        return dof * (1 - k + zz * math.sqrt(k)) ** 3
    return q(-z), q(z)


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def observations_for_line(key, a, b, azimuth, distance, sd_dist_const, sd_dist_ppm, sd_dir_sec,
                          factor=1.0):
    """A distance and a direction observation for one course (a -> b)."""
    sd_d = (sd_dist_const + distance * sd_dist_ppm * 1e-6) * factor
    # a direction is only as good as the distance allows: add the centring part
    sd_a = math.hypot(sd_dir_sec * SEC, sd_dist_const / max(distance, 1e-6)) * factor
    return [Obs(key, a, b, "dist", distance, sd_d), Obs(key, a, b, "dir", azimuth % (2 * math.pi), sd_a)]


def adjust(points, observations, max_iter=10, tol=1e-6, hold=None):
    """Run the adjustment.

    ``points``: {key: Point}.  ``observations``: list of Obs.
    ``hold``: for a consistency check with no fixed or weighted points, the
    key of the point held still (default: the first one).
    """
    res = Result()
    pts = {k: Point(p.key, p.x, p.y, p.mode, p.sd) for k, p in points.items()}
    used = {o.a for o in observations} | {o.b for o in observations}
    unknown = [k for k in pts if k in used and pts[k].mode != FIXED]
    if not any(pts[k].mode in (FIXED, WEIGHTED) for k in used):
        hold = hold if hold in used else (next(iter(sorted(used, key=str))) if used else None)
        if hold is None:
            res.warnings.append("No observations to adjust.")
            return res
        res.held = hold
        unknown = [k for k in unknown if k != hold]
    idx = {k: i for i, k in enumerate(unknown)}
    n = 2 * len(unknown)
    weighted = [k for k in unknown if pts[k].mode == WEIGHTED]
    m = len(observations) + 2 * len(weighted)
    res.dof = m - n
    if n == 0:
        res.warnings.append("Every point is fixed - nothing to adjust.")
        return res
    if res.dof < 0:
        res.warnings.append("Not enough observations for the points involved.")
        return res
    start = {k: (pts[k].x, pts[k].y) for k in pts}
    w = np.empty(m)
    for it in range(1, max_iter + 1):
        A = np.zeros((m, n))
        f = np.zeros(m)
        for r, o in enumerate(observations):
            xa, ya = pts[o.a].x, pts[o.a].y
            xb, yb = pts[o.b].x, pts[o.b].y
            dx, dy = xb - xa, yb - ya
            d2 = dx * dx + dy * dy
            d = math.sqrt(d2) if d2 > 0 else 1e-9
            if o.kind == "dist":
                f[r] = o.value - d
                ga, gb = (-dx / d, -dy / d), (dx / d, dy / d)
            else:
                f[r] = _wrap(o.value - math.atan2(dx, dy))
                ga, gb = (-dy / d2, dx / d2), (dy / d2, -dx / d2)
            if o.a in idx:
                A[r, 2 * idx[o.a]], A[r, 2 * idx[o.a] + 1] = ga
            if o.b in idx:
                A[r, 2 * idx[o.b]], A[r, 2 * idx[o.b] + 1] = gb
            w[r] = 1.0 / (o.sd * o.sd)
        r0 = len(observations)
        for j, k in enumerate(weighted):
            i = idx[k]
            A[r0 + 2 * j, 2 * i] = 1.0
            A[r0 + 2 * j + 1, 2 * i + 1] = 1.0
            f[r0 + 2 * j] = start[k][0] - pts[k].x
            f[r0 + 2 * j + 1] = start[k][1] - pts[k].y
            w[r0 + 2 * j] = w[r0 + 2 * j + 1] = 1.0 / (pts[k].sd ** 2)
        AtW = A.T * w
        N = AtW @ A
        try:
            dxv = np.linalg.solve(N, AtW @ f)
        except np.linalg.LinAlgError:
            res.warnings.append("The network can't be solved - some points aren't tied in by both "
                                "a distance and a direction, or lines don't connect.")
            return res
        for k, i in idx.items():
            pts[k].x += dxv[2 * i]
            pts[k].y += dxv[2 * i + 1]
        res.iterations = it
        if np.max(np.abs(dxv)) < tol:
            res.converged = True
            break
    # residuals and statistics at the solution
    Ninv = np.linalg.inv(N)
    v = f - A @ dxv                     # observed - adjusted after the last step
    vtpv = float(np.sum(w * v * v))
    res.sigma0 = math.sqrt(vtpv / res.dof) if res.dof > 0 else 1.0
    qvv_diag = 1.0 / w - np.einsum("ij,jk,ik->i", A, Ninv, A)
    for r, o in enumerate(observations):
        q = max(qvv_diag[r], 1e-30)
        red = q * w[r]
        res.obs.append(ObsResult(o, o.value - v[r], v[r], v[r] / math.sqrt(q) if red > 1e-6 else 0.0, red))
    for k in pts:
        if k not in used:
            continue
        p = pts[k]
        pr = PointResult(k, p.x, p.y, p.x - start[k][0], p.y - start[k][1])
        if k in idx:
            i = idx[k]
            c = Ninv[2 * i:2 * i + 2, 2 * i:2 * i + 2] * res.sigma0 ** 2
            sxx, syy, sxy = c[0, 0], c[1, 1], c[0, 1]
            t = math.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2)
            pr.a = math.sqrt(max((sxx + syy) / 2 + t, 0))
            pr.b = math.sqrt(max((sxx + syy) / 2 - t, 0))
            # azimuth (from north, clockwise) of the major axis
            pr.theta = (math.pi / 2 - 0.5 * math.atan2(2 * sxy, sxx - syy)) % math.pi
        res.points[k] = pr
    if not res.converged:
        res.warnings.append("Didn't converge in %d iterations - check for a large blunder." % max_iter)
    return res


def ellipse_points(cx, cy, a, b, theta, scale=1.0, n=36) -> List[Tuple[float, float]]:
    """Outline of an error ellipse (major axis at azimuth ``theta``)."""
    out = []
    for i in range(n + 1):
        t = 2 * math.pi * i / n
        u, v = a * scale * math.cos(t), b * scale * math.sin(t)     # along / across the major axis
        out.append((cx + u * math.sin(theta) + v * math.cos(theta),
                    cy + u * math.cos(theta) - v * math.sin(theta)))
    return out
