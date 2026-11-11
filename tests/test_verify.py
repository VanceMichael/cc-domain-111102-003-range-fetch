"""校验原语的单元测试。"""

import unittest

from app.verify import (
    is_html,
    media_type_matches,
    parse_content_range,
    strong_etag,
)


class HeaderMapStub:
    def __init__(self, value):
        self.value = value

    def get(self, name):
        return self.value if name.lower() == "etag" else None


class StrongEtagTests(unittest.TestCase):
    def test_accepts_strong(self):
        self.assertEqual(
            strong_etag(HeaderMapStub('"abc123"')), '"abc123"')

    def test_rejects_weak(self):
        self.assertIsNone(strong_etag(HeaderMapStub('W/"abc123"')))

    def test_rejects_missing_and_garbage(self):
        self.assertIsNone(strong_etag(HeaderMapStub(None)))
        self.assertIsNone(strong_etag(HeaderMapStub("abc123")))
        self.assertIsNone(strong_etag(HeaderMapStub('"unclosed')))


class ContentRangeTests(unittest.TestCase):
    def test_valid(self):
        parsed = parse_content_range("bytes 100-199/1000")
        self.assertEqual((parsed.start, parsed.end, parsed.total, parsed.length),
                         (100, 199, 1000, 100))

    def test_unknown_total(self):
        parsed = parse_content_range("bytes 0-99/*")
        self.assertIsNone(parsed.total)

    def test_rejects_bad_values(self):
        for value in (None, "", "bytes 200-100/1000", "bytes 0-99/50",
                      "0-99/1000", "bytes 0-99", "bytes x-y/z"):
            with self.subTest(value=value):
                self.assertIsNone(parse_content_range(value))


class MediaTypeTests(unittest.TestCase):
    PDF = b"%PDF-1.4\nrest"

    def test_pdf_magic(self):
        self.assertTrue(
            media_type_matches("application/pdf", self.PDF, "application/pdf"))

    def test_html_login_page_even_with_200(self):
        html = b"<!doctype html><html><body>login</body></html>"
        self.assertTrue(is_html(html, "text/html; charset=utf-8"))
        self.assertFalse(
            media_type_matches("application/pdf", html, "text/html"))

    def test_html_detected_without_content_type(self):
        html = b"\n  <HTML><form>password</form>"
        self.assertTrue(is_html(html, None))
        self.assertFalse(media_type_matches("application/pdf", html, None))

    def test_declared_type_mismatch_rejected(self):
        self.assertFalse(
            media_type_matches("application/pdf", self.PDF, "image/png"))

    def test_octet_stream_declaration_allowed(self):
        self.assertTrue(
            media_type_matches("application/pdf", self.PDF,
                               "application/octet-stream"))

    def test_unknown_type_relies_on_html_guard(self):
        body = b"binary\x00\x01data"
        self.assertTrue(
            media_type_matches("application/x-new-format", body, None))


if __name__ == "__main__":
    unittest.main()
