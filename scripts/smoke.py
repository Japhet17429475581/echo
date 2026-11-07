"""Run local end-to-end contracts; no Telegram token, public scanning or paid API required."""

import os
import sys

import pytest

if __name__ == "__main__":
    os.environ.setdefault("ECHO_TEST_LIVE_ENGINES", "1")
    sys.exit(
        pytest.main(
            [
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_smoke.py",
                "tests/test_engine_contracts.py",
                "tests/test_exports.py",
            ]
        )
    )
