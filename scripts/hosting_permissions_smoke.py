"""Run in the runtime image with a root-owned /app tmpfs to verify its bootstrap."""

import os
import stat
import tempfile
from pathlib import Path

assert os.getuid() == os.getgid() == 10001
parent = Path("/app/data")
private = parent / "echo"
assert parent.stat().st_uid == 0 and stat.S_IMODE(parent.stat().st_mode) == 0o755
assert private.stat().st_uid == private.stat().st_gid == 10001
assert stat.S_IMODE(private.stat().st_mode) == 0o700
with tempfile.TemporaryFile(dir=private) as stream:
    stream.write(b"owned storage fixture")
    stream.seek(0)
    assert stream.read() == b"owned storage fixture"
status = Path("/proc/self/status").read_text()
for name in ("CapEff", "CapPrm"):
    assert int(next(line.split()[1] for line in status.splitlines() if line.startswith(name + ":")), 16) == 0
print("hosting permissions: private storage writable, parent unchanged, UID/GID 10001, zero capabilities")
