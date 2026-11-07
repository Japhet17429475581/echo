"""Only used by tests: real engine library against a local HTTP fixture, never production site selection."""

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import io
import json
import logging
import sys
from pathlib import Path

logging.disable(logging.CRITICAL)
spec = importlib.util.spec_from_file_location(
    "runner", Path(__file__).resolve().parents[1] / "engines/runner.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
request = json.load(sys.stdin)
engine, base, path = request["engine"], request["base"], request["path"]
nick = "echo_fixture_user"
name = "Local HTTP fixture"
with contextlib.redirect_stdout(io.StringIO()):
    if engine == "sherlock":
        site = {
            "urlMain": base,
            "url": base + path + "/{}",
            "errorType": "message",
            "errorMsg": "PROFILE_ABSENT",
        }
        results = runner.check_sherlock(nick, {name: site}, 2)
        version = importlib.metadata.version("sherlock-project")
    else:
        from maigret.sites import MaigretSite

        site = MaigretSite(
            name,
            {
                "urlMain": base,
                "url": base + path + "/{username}",
                "checkType": "message",
                "presenseStrs": ["PROFILE_FOUND"],
                "absenceStrs": ["PROFILE_ABSENT"],
            },
        )
        results = asyncio.run(runner.check_maigret(nick, {name: site}, 2))
        version = importlib.metadata.version("maigret")
print(json.dumps(runner.serialize(engine, version, name, site, results[name], nick)))
