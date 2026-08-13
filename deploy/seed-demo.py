#!/usr/bin/env python3
"""Put demo content on a running Arcadia.

A freshly deployed platform is empty, and an empty storefront demos badly: the landing
page has no games to show, `/browse` shows its "nothing published yet" state, and none of
the interesting screens — the review queue, a library, a game page — have anything to put
on them. This walks the real publishing workflow to fix that.

It drives the **public gateway**, over HTTPS, with real accounts and real tokens — the same
path a browser takes. Nothing here reaches into a database or mints its own JWT, so a run
that finishes is also evidence that registration, approval, role granting, upload,
review and publishing all work against the deployment it just ran on. That is deliberate:
a seeder that wrote rows directly would leave the platform looking populated while telling
you nothing about whether it works.

Idempotent. Accounts that exist are reused, games that are already published are left
alone, so it is safe to run repeatedly against the same cluster.

    export ARCADIA_API=https://api.arcadia.aptcodegen.online
    export SUPER_ADMIN_EMAIL=... SUPER_ADMIN_PASSWORD=...
    python3 deploy/seed-demo.py

Credentials come from the environment and are never written here. To read them from the
cluster instead of typing them:

    eval $(kubectl -n arcadia get secret arcadia-secrets -o json | python3 -c "
    import sys,json,base64
    d=json.load(sys.stdin)['data']
    for k in ('SUPER_ADMIN_EMAIL','SUPER_ADMIN_PASSWORD'):
        print(f'export {k}={base64.b64decode(d[k]).decode()}')")

The demo accounts all share one password, printed at the end so it can be handed to
whoever is demonstrating. That is fine for a demo platform and is not fine anywhere else.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import urllib.error
import urllib.request
import uuid
import zlib

API = os.environ.get("ARCADIA_API", "https://api.arcadia.aptcodegen.online").rstrip("/")
ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "")

# One password for every seeded account, so a demo can be driven without a password list.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "Demo-Arcadia-2026!")

TIMEOUT = 30


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: object):
        super().__init__(f"{method} {path} -> {status}: {body}")
        self.status = status
        self.body = body


def call(
    method: str,
    path: str,
    *,
    token: str = "",
    body: dict | None = None,
    expect: tuple[int, ...] = (200, 201, 204),
) -> object:
    """One JSON request against the gateway."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code

    parsed: object = None
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw[:200].decode(errors="replace")

    if status not in expect:
        raise ApiError(method, path, status, parsed)
    return parsed


def upload(path: str, *, token: str, filename: str, content: bytes, content_type: str,
           fields: dict[str, str]) -> dict:
    """A multipart upload, built by hand to keep this script dependency-free."""
    boundary = f"----arcadia{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    payload = b"".join(parts)

    request = urllib.request.Request(f"{API}{path}", data=payload, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ApiError("POST", path, error.code, error.read()[:300].decode(errors="replace")) from None


def cover_png(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> bytes:
    """A vertical gradient, as a real PNG.

    Generated rather than committed: five binary files in the repository to make a demo look
    populated is a poor trade, and media-service sniffs the leading bytes of an upload, so
    this has to be a genuine PNG rather than something merely named .png.
    """
    rows = bytearray()
    for y in range(height):
        ratio = y / max(height - 1, 1)
        pixel = bytes(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        rows.append(0)  # per-scanline filter: none
        rows.extend(pixel * width)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit truecolour
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def login(email: str, password: str) -> str:
    result = call("POST", "/auth/v1/auth/login", body={"email": email, "password": password})
    return result["access_token"]  # type: ignore[index]


def ensure_account(admin_token: str, email: str, display_name: str, role: str) -> tuple[str, str]:
    """An active account with the role asked for, whether or not it existed already.

    Registration lands in PENDING by design — an admin decides — so seeding a usable account
    means walking that decision too, exactly as the admin screen would.
    """
    try:
        return "", login(email, DEMO_PASSWORD)
    except ApiError as error:
        if error.status not in (401, 403, 404):
            raise

    try:
        created = call(
            "POST",
            "/auth/v1/auth/register",
            body={"email": email, "password": DEMO_PASSWORD, "display_name": display_name},
        )
        user_id = created["user_id"]  # type: ignore[index]
    except ApiError as error:
        # Already registered but not approved, or approved with another password: find them
        # in the directory and carry on rather than failing the whole run.
        if error.status != 409:
            raise
        directory = call("GET", "/auth/v1/admin/users", token=admin_token)
        matches = [u for u in directory if u["email"] == email]  # type: ignore[union-attr]
        if not matches:
            raise
        user_id = matches[0]["user_id"]

    call(
        "POST",
        f"/auth/v1/registrations/{user_id}/decide",
        token=admin_token,
        body={"approve": True, "note": "seeded demo account"},
    )
    if role != "BASIC_USER":
        call(
            "POST",
            f"/auth/v1/admin/users/{user_id}/grant-role",
            token=admin_token,
            body={"new_role": role},
        )
    return user_id, login(email, DEMO_PASSWORD)


GAMES = [
    {
        "title": "Neon Drift",
        "description": (
            "A street racer built around one idea: the corner you take badly is the corner you "
            "remember. Sixty tracks, no loading screens between them, and a rewind that costs "
            "you position rather than time."
        ),
        "min_requirements": "4 GB RAM, GTX 1050, 12 GB free",
        "genres": ["Racing", "Indie"],
        "price": 890_000,
        "suggested": 950_000,
        "colours": ((124, 58, 237), (236, 72, 153)),
    },
    {
        "title": "Paper Kingdoms",
        "description": (
            "A strategy game on a folded map. Every province you take has to be creased into "
            "place, and a kingdom stretched too far tears along the seams you made yourself."
        ),
        "min_requirements": "8 GB RAM, integrated graphics, 6 GB free",
        "genres": ["Strategy", "Simulation"],
        "price": 1_240_000,
        "suggested": 1_300_000,
        "colours": ((251, 146, 60), (244, 63, 94)),
    },
    {
        "title": "Deep Signal",
        "description": (
            "You are the only listener on a station that stopped answering four years ago. "
            "A survival horror game with no combat and one working torch."
        ),
        "min_requirements": "8 GB RAM, GTX 1660, 30 GB free",
        "genres": ["Horror", "Adventure"],
        "price": 1_580_000,
        "suggested": 1_650_000,
        "colours": ((14, 165, 233), (30, 41, 59)),
    },
    {
        "title": "Garden of Forking Paths",
        "description": (
            "A puzzle game about a garden that rearranges itself when you are not looking. "
            "Ninety rooms, each solvable three ways, none of them the way you expect."
        ),
        "min_requirements": "4 GB RAM, integrated graphics, 3 GB free",
        "genres": ["Puzzle", "Indie"],
        "price": 620_000,
        "suggested": 700_000,
        "colours": ((34, 197, 94), (16, 185, 129)),
    },
    {
        "title": "Ironworks",
        "description": (
            "Build a factory in a valley that floods every spring. A logistics game where the "
            "map fights back on a schedule you can read but cannot change."
        ),
        "min_requirements": "16 GB RAM, GTX 1660, 20 GB free",
        "genres": ["Simulation", "Strategy"],
        "price": 1_890_000,
        "suggested": 1_950_000,
        "colours": ((99, 102, 241), (14, 116, 144)),
    },
]


def published_titles() -> set[str]:
    listing = call("GET", "/catalog/v1/games?limit=100")
    return {game["title"] for game in listing["items"]}  # type: ignore[index,union-attr]


def publish_game(spec: dict, developer: str, support: str) -> str:
    """One game, from DRAFT to PUBLISHED, through every state the workflow requires."""
    created = call(
        "POST",
        "/catalog/v1/games",
        token=developer,
        body={
            "title": spec["title"],
            "description": spec["description"],
            "min_requirements": spec["min_requirements"],
            "genres": spec["genres"],
        },
    )
    game_id = created["id"]  # type: ignore[index]

    # Cover art first: it has to be attached before the game is submitted, because a game in
    # review is no longer the developer's to edit.
    art = upload(
        "/media/v1/media",
        token=developer,
        filename=f"{game_id}.png",
        content=cover_png(960, 540, *spec["colours"]),
        content_type="image/png",
        fields={"kind": "IMAGE", "reference_id": game_id},
    )
    call(
        "POST",
        f"/catalog/v1/games/{game_id}/media",
        token=developer,
        body={"kind": "TEASER", "media_ref": art["url"]},
    )

    steps = [
        ("POST", f"/catalog/v1/games/{game_id}/versions",
         {"version": "1.0.0", "file_ref": f"builds/{game_id}/1.0.0.zip", "size_bytes": 2048}, developer),
        ("POST", f"/catalog/v1/games/{game_id}/submit", None, developer),
        ("POST", f"/catalog/v1/games/{game_id}/review/start", None, support),
        ("POST", f"/catalog/v1/games/{game_id}/review/approve", {"note": "Plays well, ships clean."}, support),
        ("POST", f"/catalog/v1/games/{game_id}/suggest-price", {"amount_minor": spec["suggested"]}, support),
        ("POST", f"/catalog/v1/games/{game_id}/price", {"amount_minor": spec["price"]}, developer),
        ("POST", f"/catalog/v1/games/{game_id}/publish", None, developer),
    ]
    for method, path, payload, token in steps:
        call(method, path, token=token, body=payload)
    return game_id


def main() -> int:
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        print("SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD must be set", file=sys.stderr)
        return 2

    print(f"seeding {API}")
    admin_token = login(ADMIN_EMAIL, ADMIN_PASSWORD)
    print("  signed in as the super admin")

    accounts = [
        ("developer@arcadia.example", "Wren Ashcroft", "DEVELOPER"),
        ("support@arcadia.example", "Sam Okafor", "SUPPORT"),
        ("player@arcadia.example", "Nadia Farr", "BASIC_USER"),
    ]
    tokens: dict[str, str] = {}
    for email, name, role in accounts:
        _, token = ensure_account(admin_token, email, name, role)
        tokens[role] = token
        print(f"  {role:<10} {email}")

    existing = published_titles()
    added = 0
    for spec in GAMES:
        if spec["title"] in existing:
            print(f"  = {spec['title']} (already published)")
            continue
        publish_game(spec, tokens["DEVELOPER"], tokens["SUPPORT"])
        print(f"  + {spec['title']}")
        added += 1

    total = len(published_titles())
    print(f"\n{added} published, {total} on the storefront")
    print(f"demo accounts all use the password: {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as error:
        print(f"\nfailed: {error}", file=sys.stderr)
        sys.exit(1)
