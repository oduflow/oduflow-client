#!/usr/bin/python3
"""Read-only local application/auth readiness; never emit credentials or responses."""

import base64
import http.client
import json
import os
import stat
import sys
import time

import tomllib

TIMEOUT = 240
TARGETS = {"oduflow": ("172.17.0.1", 8000), "paseo": ("127.0.0.1", 6767)}


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1048576
        ):
            raise ValueError("private_configuration_invalid")
        return stream.read(1048577).decode("utf-8")


def credentials():
    config = tomllib.loads(read_private("/etc/oduflow/oduflow.toml"))
    password = config["team"]["1"]["ui_password"]
    entries = [
        line.split("=", 1)[1]
        for line in read_private("/etc/paseo/credentials.env").splitlines()
        if line.startswith("PASEO_PASSWORD=")
    ]
    if (
        not isinstance(password, str)
        or len(password) < 16
        or len(entries) != 1
        or len(entries[0]) < 16
        or any(character.isspace() for character in entries[0])
    ):
        raise ValueError("private_configuration_invalid")
    return {
        "oduflow": "Basic " + base64.b64encode(("admin:" + password).encode()).decode(),
        "paseo": "Bearer " + entries[0],
    }


def checks(auth):
    return (
        ("oduflow_health", "oduflow", "/healthz", 200, None),
        ("oduflow_auth_required", "oduflow", "/api/productions", 401, None),
        ("oduflow_authenticated", "oduflow", "/api/productions", 200, auth["oduflow"]),
        ("paseo_health", "paseo", "/api/health", 200, None),
        ("paseo_auth_required", "paseo", "/api/status", 401, None),
        ("paseo_authenticated", "paseo", "/api/status", 200, auth["paseo"]),
    )


def request_status(service, path, authorization, timeout):
    # Fixed local targets: no ambient HTTP proxy and no credential-bearing redirect.
    connection = http.client.HTTPConnection(*TARGETS[service], timeout=timeout)
    try:
        connection.request(
            "GET", path, headers={"Authorization": authorization} if authorization else {}
        )
        response = connection.getresponse()
        return response.status
    finally:
        connection.close()


def wait_ready(auth, timeout=TIMEOUT, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + min(max(timeout, 0), TIMEOUT)
    probes = checks(auth)
    while True:
        failures = []
        for code, service, path, expected, authorization in probes:
            remaining = deadline - clock()
            if remaining <= 0:
                failures.append(code)
                continue
            try:
                actual = request_status(service, path, authorization, min(3, remaining))
            except Exception:
                actual = None
            if actual != expected:
                failures.append(code)
        if not failures:
            return True, []
        remaining = deadline - clock()
        if remaining <= 0:
            return False, failures
        sleep(min(3, remaining))


def main():
    try:
        ready, failures = wait_ready(credentials())
    except Exception:
        ready, failures = False, ["private_configuration_invalid"]
    print(
        json.dumps(
            {
                "changed": False,
                "comment": "client_application_ready checks=6"
                if ready
                else "client_application_not_ready checks=" + ",".join(failures),
            }
        )
    )
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
