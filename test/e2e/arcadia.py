"""A thin client for the running platform.

Deliberately built on urllib rather than httpx or requests: these tests should need as
little installed as possible, because the reason to run them is to check the *platform*,
and a dependency that fails to install is a distraction from that.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

# Matches deploy/compose/.env.example. If the tests fail to authenticate at all, this is
# the first thing to compare against your .env.
JWT_SECRET = "local-development-jwt-secret-change-me-please"

# Required by every service, Go and Python alike. A token without them is rejected — see
# the note in each service's platform/config.py.
ISSUER = "arcadia-auth"
AUDIENCE = "arcadia"

WALLET = "http://localhost:8080"
PAYMENT = "http://localhost:8081"
CATALOG = "http://localhost:8082"
ORDER = "http://localhost:8083"
MEDIA = "http://localhost:8084"
AUTH = "http://localhost:8085"
NOTIFICATION = "http://localhost:8086"
MARKETPLACE = "http://localhost:8087"
REVIEW = "http://localhost:8088"
FESTIVAL = "http://localhost:8089"
COMMUNITY = "http://localhost:8091"
SEARCH = "http://localhost:8092"

# The gateway. Every address above is what a service listens on directly; this is the one
# a browser is meant to use. Both are kept, because the point of test_11_gateway.py is to
# compare them.
GATEWAY = "http://localhost:8090"

# The platform's own wallet, from PLATFORM_USER_ID.
PLATFORM_USER = "00000000-0000-4000-8000-000000000001"

KAFKA_CONTAINER = "arcadia-kafka"
POSTGRES_CONTAINER = "arcadia-postgres"
MEDIA_CONTAINER = "arcadia-media"
MINIO_CONTAINER = "arcadia-minio"
MINIO_BUCKET = "arcadia-media"
MEDIA_STORAGE_ROOT = "/var/lib/arcadia/media"

# MinIO's root credentials, matching deploy/compose/.env.example — the same way this file
# already hardcodes JWT_SECRET. Root rather than the media service's own key because listing a
# bucket is deliberately not in that key's policy.
MINIO_ROOT_USER = "arcadia-root"
MINIO_ROOT_PASSWORD = "local-development-minio-root-change-me"


def new_id() -> str:
    """A fresh UUID.

    The wallet service stores user_id as `uuid`, so anything else is rejected — and using a
    fresh one per run is what lets these tests be run repeatedly without `make nuke`.
    """
    return str(uuid.uuid4())


def token(user_id: str, role: str = "BASIC_USER", *, scopes: list[str] | None = None) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "role": role,
            "typ": "access",
            "scopes": scopes or [],
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(hours=2),
        },
        JWT_SECRET,
        algorithm="HS256",
    )


@dataclass
class Response:
    status: int
    body: dict | list | None
    headers: dict[str, str]
    raw: bytes

    def __repr__(self) -> str:
        return f"<{self.status} {json.dumps(self.body)[:300] if self.body else self.raw[:120]!r}>"


def call(
    method: str,
    url: str,
    *,
    user: str | None = None,
    role: str = "BASIC_USER",
    scopes: list[str] | None = None,
    bearer: str | None = None,
    body: dict | None = None,
    key: str | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 30.0,
) -> Response:
    """One request. Never raises for an HTTP status — the tests assert on it.

    `user` mints a token here; `bearer` presents one minted elsewhere. Almost every test wants the
    first — they are checking the platform, not the issuer — but `test_00_identity.py` needs the
    second, because the only way to know the auth service produces acceptable tokens is to use one.
    """
    request = urllib.request.Request(url, method=method)
    if bearer:
        request.add_header("Authorization", f"Bearer {bearer}")
    elif user:
        request.add_header("Authorization", f"Bearer {token(user, role, scopes=scopes)}")
    if key:
        request.add_header("Idempotency-Key", key)

    data = raw_body
    if body is not None:
        data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
    elif content_type:
        request.add_header("Content-Type", content_type)

    try:
        with urllib.request.urlopen(request, data, timeout=timeout) as response:
            payload = response.read()
            return Response(
                response.status,
                _decode(payload, response.headers.get("content-type", "")),
                {k.lower(): v for k, v in response.headers.items()},
                payload,
            )
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return Response(
            exc.code,
            _decode(payload, exc.headers.get("content-type", "")),
            {k.lower(): v for k, v in exc.headers.items()},
            payload,
        )


def _decode(payload: bytes, content_type: str):
    if not payload:
        return None
    if "json" not in content_type:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def multipart(
    url: str,
    *,
    user: str,
    role: str,
    file: tuple[str, bytes, str],
    fields: dict[str, str] | None = None,
) -> Response:
    """A multipart upload, hand-built to avoid a dependency for one request."""
    filename, data, file_type = file
    boundary = f"----arcadia{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in (fields or {}).items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {file_type}\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return call(
        "POST",
        url,
        user=user,
        role=role,
        raw_body=b"".join(parts),
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def provision_wallet(user_id: str) -> dict:
    """Ensure a wallet exists, by asking for it.

    `GET /v1/wallets/me` provisions on first access, so this is all it takes.

    There **is** an auth service now, and `test_00_identity.py` proves the event path works: a
    registration provisions a wallet with no HTTP call at all. This helper stays because most tests
    here want a funded user in one line, not a registration, an approval and a login — the flow
    they are testing starts after all that.

    It also stays because it is the honest thing for the suite to depend on: first-access
    provisioning exists precisely so nobody is blocked waiting for an event, and a test that
    relied on the event's timing would be testing Kafka's latency rather than the platform.
    """
    response = call("GET", f"{WALLET}/v1/wallets/me", user=user_id)
    assert response.status == 200, f"could not provision a wallet for {user_id}: {response}"
    return response.body


def psql(database: str, sql: str) -> str:
    """Run a query as the owning role, for the platform-health assertions.

    Reading a service's database from a test is normally the wrong thing to do. It is right
    here for one narrow purpose: proving invariants nobody's API exposes — that every
    dead-letter topic is empty, every outbox drained, and every balance still equals the sum
    of its ledger.
    """
    role = f"{database}_user"
    result = subprocess.run(
        ["docker", "exec", POSTGRES_CONTAINER, "psql", "-U", role, "-d", f"arcadia_{database}",
         "-tAc", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def media_backend() -> str:
    """Which object store the running media service was configured with.

    Read off the container rather than from .env, so an assertion follows the stack that is
    actually up instead of whatever the file says today.
    """
    result = subprocess.run(
        ["docker", "inspect", MEDIA_CONTAINER, "--format",
         "{{range .Config.Env}}{{println .}}{{end}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("STORAGE_BACKEND="):
            return line.split("=", 1)[1].strip()
    return "filesystem"


def stored_object_keys() -> set[str]:
    """Every key the media service's object store actually holds.

    Follows the backend rather than assuming one. With `STORAGE_BACKEND=s3` the bytes are in a
    MinIO bucket and the media-data volume is empty — the health assertion that walked the volume
    passed for months and then reported every file on the platform as missing the day the backend
    changed, which was accurate about the volume and wrong about the platform.

    The MinIO container ships `mc`, so no extra image is needed. Credentials go in as
    `MC_HOST_<alias>` rather than through `mc alias set`: the container's own `local` alias is
    unauthenticated — enough for the `mc ready` healthcheck, not enough to list a private bucket —
    and reconfiguring it would write the root password into the container's config file.
    """
    if media_backend() == "s3":
        result = subprocess.run(
            ["docker", "exec",
             "-e", f"MC_HOST_probe=http://{MINIO_ROOT_USER}:{MINIO_ROOT_PASSWORD}@localhost:9000",
             MINIO_CONTAINER, "mc", "--json", "ls", "--recursive", f"probe/{MINIO_BUCKET}"],
            capture_output=True,
            text=True,
            # Checked below, with a message that names the bucket. `check=True` would raise a
            # CalledProcessError whose text is the whole argv — root password included.
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"could not list the media bucket: {result.stderr.strip()}")
        return {
            json.loads(line)["key"] for line in result.stdout.splitlines() if line.strip()
        }

    result = subprocess.run(
        ["docker", "exec", MEDIA_CONTAINER, "find", MEDIA_STORAGE_ROOT, "-type", "f"],
        capture_output=True,
        text=True,
        check=True,
    )
    prefix = MEDIA_STORAGE_ROOT.rstrip("/") + "/"
    return {
        line[len(prefix):] for line in result.stdout.splitlines()
        if line.startswith(prefix) and "/.tmp/" not in line
    }


def topic_message_count(topic: str) -> int:
    result = subprocess.run(
        ["docker", "exec", KAFKA_CONTAINER, "kafka-run-class.sh",
         "kafka.tools.GetOffsetShell", "--bootstrap-server", "localhost:9092",
         "--topic", topic],
        capture_output=True,
        text=True,
        # A topic that was never created exits non-zero, and for these assertions that genuinely
        # means zero messages — an empty dead-letter topic is the answer they want.
        check=False,
    )
    total = 0
    for line in result.stdout.strip().splitlines():
        parts = line.rsplit(":", 1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            total += int(parts[1])
    return total
