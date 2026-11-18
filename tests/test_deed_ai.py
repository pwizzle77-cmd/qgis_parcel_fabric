import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "parcel_fabric"))

import pytest

from core import cogo as C
from core import deed_ai as A
from core import ollama as O

DEED = """WARRANTY DEED. This deed made the 2nd day of April 1998 between JOHN DOE, grantor, and
JANE ROE, grantee. Witnesseth that the grantor ... the following land in Columbia County, Florida:

Commence at the Northwest corner of Section 12, Township 4 South, Range 16 East; thence S 00°12'30" E
along the West line of said Section, 400.00 feet to the Point of Beginning; thence N 89°47'30" E,
200.00 feet; thence S 00°12'30" E, 100.00 feet; thence S 89°47'30" W, 200.00 feet; thence
N 00°12'30" W, 100.00 feet to the Point of Beginning. Containing 0.46 acres, more or less.

Subject to easements of record. IN WITNESS WHEREOF the grantor has signed."""


def test_find_description():
    d = A.find_description(DEED)
    assert d.startswith("Commence at the Northwest corner")
    assert d.endswith("Containing 0.46 acres, more or less.")
    assert "WITNESS" not in d and "grantee" not in d
    assert A.find_description("no description here") == ""


def test_check_call_accepts_quoted_line():
    call = {"quote": "thence N 89°47'30\" E, 200.00 feet", "kind": "line",
            "bearing": "N 89°47'30\" E", "distance": 200.0}
    c = A.check_call(call, DEED)
    assert c.kind == C.LINE and c.distance == 200.0


def test_check_call_rejects_invented_numbers_and_quotes():
    with pytest.raises(ValueError, match="quote"):
        A.check_call({"quote": "thence N 10 E, 50 feet", "kind": "line", "bearing": "N 10 E",
                      "distance": 50}, DEED)
    with pytest.raises(ValueError, match="distance"):
        A.check_call({"quote": "thence N 89°47'30\" E, 200.00 feet", "kind": "line",
                      "bearing": "N 89°47'30\" E", "distance": 300.0}, DEED)
    with pytest.raises(ValueError, match="bearing number"):
        A.check_call({"quote": "thence N 89°47'30\" E, 200.00 feet", "kind": "line",
                      "bearing": "N 89°47'31\" E", "distance": 200.0}, DEED)


def test_check_call_curve():
    text = "thence along a curve to the right having a radius of 100.00 feet, an arc length of 157.08 feet, " \
           "chord bearing N 45°00'00\" E"
    c = A.check_call({"quote": text, "kind": "curve", "bearing": "N 45°00'00\" E", "radius": 100.0,
                      "arc_length": 157.08, "side": "right", "bearing_is": "chord"}, text)
    assert c.kind == C.CURVE and c.radius == 100.0 and c.arc_length == 157.08


class _Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "qwen2.5vl:7b"}, {"name": "deepseek-ocr:3b"}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = json.dumps({"calls": []}) if req.get("format") else "OCR TEXT"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"message": {"content": content}}).encode())


def test_ollama_client_against_fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % srv.server_port
    models = O.list_models(url)
    assert O.pick(models, "deepseek-ocr") == "deepseek-ocr:3b"
    assert O.chat(url, "deepseek-ocr:3b", "Free OCR.", ["aGk="]) == "OCR TEXT"
    assert O.chat_json(url, "qwen2.5vl:7b", "x", A.CALL_SCHEMA) == {"calls": []}
    srv.shutdown()
    with pytest.raises(O.OllamaError, match="Can't reach"):
        O.list_models("http://127.0.0.1:9")
