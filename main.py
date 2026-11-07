"""Root entrypoint for hosting panels; dependencies stay outside the mounted source tree."""

import os
import sys
from pathlib import Path


def main():
    interpreter = Path("/opt/echo/bin/python")
    if os.name == "posix" and interpreter.is_file() and sys.prefix != "/opt/echo":
        os.execv(str(interpreter), [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from echo.main import main as application

    if len(sys.argv) == 1:
        sys.argv.append("serve")
    application()


if __name__ == "__main__":
    main()
