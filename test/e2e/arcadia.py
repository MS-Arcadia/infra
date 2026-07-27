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

# The platform's own wallet, from PLATFORM_USER_ID.
PLATFORM_USER = "00000000-0000-4000-8000-000000000001"

KAFKA_CONTAINER = "arcadia-kafka"
POSTGRES_CONTAINER = "arcadia-postgres"


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
    body: dict | None = None,
    key: str | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 30.0,
) -> Response:
    """One request. Never raises for an HTTP status — the tests assert on it."""
    request = urllib.request.Request(url, method=method)
    if user:
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

    `GET /v1/wallets/me` provisions on first access, so this is all it takes. In production a
    wallet is normally created by the wallet service's UserRegistered consumer, driven by the
    Auth service — but there is no Auth service in this stack, and first-access provisioning
    exists precisely so a user is never blocked waiting for an event.

    This suite deliberately does *not* publish UserRegistered to Kafka to set up. It tried,
    and `kafka-console-producer.sh` hangs under `docker exec` — five minutes of runtime for
    something a 40ms HTTP call does. The consumer itself is covered by the wallet service's
    own tests; what this suite is for is the flows that cross services.
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


def topic_message_count(topic: str) -> int:
    result = subprocess.run(
        ["docker", "exec", KAFKA_CONTAINER, "kafka-run-class.sh",
         "kafka.tools.GetOffsetShell", "--bootstrap-server", "localhost:9092",
         "--topic", topic],
        capture_output=True,
        text=True,
    )
    total = 0
    for line in result.stdout.strip().splitlines():
        parts = line.rsplit(":", 1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            total += int(parts[1])
    return total
