"""Prepare private directories on mounted volumes, then run ECHO as UID 10001."""

import logging
import os
import stat
import sys

from echo.config import settings
from echo.diagnostics import failure

UID = GID = 10001
log = logging.getLogger("echo")


def prepare_directory(path):
    # Only the configured private directory is changed, never its mount parent or children.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.fstat(descriptor)
        if os.geteuid() == 0 and (current.st_uid != UID or current.st_gid != GID):
            os.fchown(descriptor, UID, GID)
        if stat.S_IMODE(current.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    command = sys.argv[1:]
    if not command:
        command = ["/opt/echo/bin/python", "-m", "echo.main", "serve"]
    stage = "configuration"
    try:
        if not {"-h", "--help"}.intersection(command):
            config = settings()
            for name, path in (("data_directory", config.data_dir), ("temp_directory", config.temp_dir)):
                stage = name
                prepare_directory(path)
        stage = "drop_privileges"
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(GID)
            os.setuid(UID)
            os.environ["HOME"] = "/home/echo"
        stage = "exec"
        os.execvpe(command[0], command, os.environ)
    except Exception as error:
        failure(log, "startup_failed", stage, error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
