#!/usr/bin/env python3
"""Repository entrypoint for the canonical Salt-deployed helper."""

from pathlib import Path

_source = (
    Path(__file__).resolve().parents[1] / "salt/states/client_production/files/publication-guard.py"
)
exec(compile(_source.read_bytes(), str(_source), "exec"), globals())
