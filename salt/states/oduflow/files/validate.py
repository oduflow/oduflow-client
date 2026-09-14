#!/usr/bin/python3
"""Validate published Oduflow settings without bootstrapping Docker or logging secrets."""

import json
import sys

try:
    from oduflow.settings import Settings

    settings = Settings.from_toml(
        sys.argv[1] if len(sys.argv) == 2 else "/etc/oduflow/oduflow.toml"
    )
    settings.validate()
except Exception:
    sys.exit("Oduflow configuration validation failed; inspect protected configuration locally")
print(json.dumps({"changed": False, "comment": "Oduflow configuration validated"}))
