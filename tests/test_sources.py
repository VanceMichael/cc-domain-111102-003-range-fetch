import unittest
import json
from app.wire import Replay

from app.attachment import AttachmentEndpoint
class DomainTests(unittest.TestCase):
    def test_range(self):
        r=AttachmentEndpoint().fetch("A","bytes=0-7")
        self.assertEqual(r.status,206);self.assertEqual(len(r.body),8)
    def test_out_of_range(self):
        self.assertEqual(AttachmentEndpoint().fetch("A","bytes=99999-").status,416)
    def test_if_range(self):
        self.assertEqual(AttachmentEndpoint().fetch("A","bytes=0-7",'"old"').status,200)


class WireTests(unittest.TestCase):
    def test_unknown_route(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            p=Path(d)/"routes.json";p.write_text("{}")
            self.assertEqual(Replay(p).get("https://missing.invalid").status,404)
