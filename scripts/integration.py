"""Test-container entry point: migrate the dedicated DB, then run contracts."""

import subprocess
import sys

if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    subprocess.run([sys.executable, "-m", "alembic", "check"], check=True)
    status = subprocess.call(
        [sys.executable, "-m", "pytest", "-q", "--capture=tee-sys", "-p", "no:cacheprovider"]
    )
    if status:
        sys.exit(status)
    sys.exit(subprocess.call([sys.executable, "scripts/hosting_smoke.py"]))
