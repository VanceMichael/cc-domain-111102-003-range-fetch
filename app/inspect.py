from pathlib import Path
from app.wire import Replay
from app.attachment import AttachmentEndpoint
r=AttachmentEndpoint().fetch("A","bytes=0-7")
print(r.status,r.headers,len(r.body))
