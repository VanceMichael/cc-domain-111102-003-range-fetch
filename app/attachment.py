from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from app.wire import Reply

class AttachmentEndpoint:
    def __init__(self, root="fixtures"):
        self.root = Path(root)
        self.documents = json.loads((self.root / "documents.json").read_text())
        self.calls = []

    def fetch(self, document_id, range_header=None, if_range=None, behavior="normal"):
        doc = self.documents[document_id]
        data = (self.root / doc["file"]).read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        headers = {"ETag": etag, "Content-Type": doc["media_type"], "Content-Length": str(len(data))}
        self.calls.append((document_id, range_header, if_range))
        if behavior == "expired": return Reply(403, {}, b"expired")
        if behavior == "login": return Reply(200, {"Content-Type":"text/html"}, b"<html>login</html>")
        if behavior == "ignore" or not range_header or if_range and if_range != etag:
            return Reply(200, headers, data)
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
        if not match: return Reply(400, {}, b"")
        start = int(match[1]); end = min(int(match[2]) if match[2] else len(data)-1, len(data)-1)
        if start >= len(data) or end < start:
            return Reply(416, {"Content-Range":f"bytes */{len(data)}"}, b"")
        part = data[start:end+1]
        headers.update({"Content-Range":f"bytes {start}-{end}/{len(data)}", "Content-Length":str(len(part))})
        if behavior == "short": part = part[:-1]
        if behavior == "wrong_range": headers["Content-Range"] = f"bytes 0-{end}/{len(data)}"
        return Reply(206, headers, part)

    def refresh(self, document_id):
        if document_id not in self.documents: raise KeyError(document_id)
        return f"https://attachment.invalid/{document_id}?temporary=renewed"
