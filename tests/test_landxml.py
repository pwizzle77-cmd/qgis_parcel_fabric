import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import landxml as X
from core import legal_writer as W

DOC = """<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
 <CgPoints><CgPoint name="1">1000 2000</CgPoint><CgPoint name="2">1000 2200</CgPoint></CgPoints>
 <Parcels>
  <Parcel name="Lot 1" area="20000">
   <CoordGeom>
    <Line><Start pntRef="1"/><End pntRef="2"/></Line>
    <Line><Start>1000 2200</Start><End>900 2200</End></Line>
    <Line><Start>900 2200</Start><End>900 2000</End></Line>
    <Line><Start>900 2000</Start><End>1000 2000</End></Line>
   </CoordGeom>
  </Parcel>
  <Parcel name="Lot 2">
   <CoordGeom>
    <Line><Start>0 0</Start><End>100 0</End></Line>
    <Curve rot="cw" radius="100"><Start>100 0</Start><Center>0 0</Center><End>0 100</End></Curve>
    <Line><Start>0 100</Start><End>0 0</End></Line>
   </CoordGeom>
  </Parcel>
 </Parcels>
</LandXML>"""


def test_read_lines_and_point_refs():
    parcels = X.read(DOC)
    assert [p[0] for p in parcels] == ["Lot 1", "Lot 2"]
    name, segs, attrs = parcels[0]
    assert segs[0] == ("line", (2000.0, 1000.0), (2200.0, 1000.0))     # northing easting -> x, y
    assert attrs["area"] == "20000"
    assert abs(W.signed_area(segs)) == pytest.approx(20000)


def test_read_curve_mid_point():
    _n, segs, _a = X.read(DOC)[1]
    kind, s, m, e = segs[1]
    assert kind == "arc"
    assert math.hypot(*m) == pytest.approx(100)
    assert m[0] == pytest.approx(100 * math.cos(math.radians(45)))
    assert abs(W.signed_area(segs)) == pytest.approx(math.pi * 100 * 100 / 4)


def test_refuses_entities():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><LandXML>&a;</LandXML>'
    with pytest.raises(X.LandXMLError):
        X.read(evil)
