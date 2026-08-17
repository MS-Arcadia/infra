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

import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import UTC, datetime, timedelta

API = os.environ.get("ARCADIA_API", "https://api.arcadia.aptcodegen.online").rstrip("/")
ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "")

# One password for every seeded account, so a demo can be driven without a password list.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "Demo-Arcadia-2026!")

TIMEOUT = 60


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
    # 202 belongs here: placing an order starts a saga across the wallet and the catalogue,
    # so the order service accepts the request rather than reporting it done.
    expect: tuple[int, ...] = (200, 201, 202, 204),
    headers: dict[str, str] | None = None,
) -> object:
    """One JSON request against the gateway."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)

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


COMMONS_FILE = "https://commons.wikimedia.org/wiki/Special:FilePath/{name}?width=1600"
COMMONS_UA = "ArcadiaDemoSeeder/1.0 (https://arcadia.aptcodegen.online; course demo covers)"


def download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": COMMONS_UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def cover_png(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> bytes:
    """A vertical gradient, as a real PNG.

    Fallback only: covers are photographs from Wikimedia Commons. Generated rather than
    committed, and media-service sniffs the leading bytes of an upload, so this has to be
    a genuine PNG rather than something merely named .png.
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


def cover_bytes(spec: dict) -> tuple[bytes, str, str]:
    """Photograph for this game, or a gradient if Commons is unreachable.

    Returns (bytes, content_type, filename). Wikimedia Commons is used rather than a
    Google scrape: the files are real photographs with a licence that allows this, and
    the seeder stays dependency-free.
    """
    name = spec.get("commons")
    if name:
        url = COMMONS_FILE.format(name=urllib.parse.quote(name))
        try:
            data = download_bytes(url)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            print(f"  ! {spec['title']}: cover download failed ({error})")
        else:
            if data[:3] == b"\xff\xd8\xff":
                return data, "image/jpeg", "cover.jpg"
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data, "image/png", "cover.png"
            print(f"  ! {spec['title']}: Commons did not return an image")
    return cover_png(960, 540, *spec["colours"]), "image/png", "cover.png"


def login(email: str, password: str) -> str:
    result = call("POST", "/auth/v1/auth/login", body={"email": email, "password": password})
    return result["access_token"]  # type: ignore[index]


def subject_of(token: str) -> str:
    """The user id inside an access token.

    Read, not verified — this script holds no signing key and does not need one. The token
    was just issued by the service to this caller, and the id is only used to ask "have I
    already left a review", so a forged one would fool nobody but the forger.
    """
    payload = token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))["sub"]


def ensure_account(admin_token: str, email: str, display_name: str, role: str) -> tuple[str, str]:
    """An active account with the role asked for, whether or not it existed already.

    Registration lands in PENDING by design — an admin decides — so seeding a usable account
    means walking that decision too, exactly as the admin screen would.
    """
    try:
        token = login(email, DEMO_PASSWORD)
        return subject_of(token), token
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
        "commons": "Night city traffic (Unsplash).jpg",
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
        "commons": "Cranes made by Origami paper.jpg",
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
        "commons": "CSIRO ScienceImage 4350 CSIROs Parkes Radio Telescope with moon in the background.jpg",
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
        "commons": "Hampton Court hedge maze.jpg",
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
        "commons": "DSC03739 Blast Furnace 1 at Night, voestalpine Donawitz, 2026-02.jpg",
    },
    {
        "title": "Hollow Signal",
        "description": (
            "An abandoned station in Saturn orbit. The signal you keep hearing might be your "
            "own voice, ten years from now."
        ),
        "min_requirements": "8 GB RAM, GTX 1660, 18 GB free",
        "genres": ["Adventure", "Mystery"],
        "price": 720_000,
        "suggested": 780_000,
        "colours": ((56, 189, 248), (15, 23, 42)),
        "commons": "Saturn during Equinox.jpg",
    },
    {
        "title": "Lantern Way",
        "description": (
            "A long walk with a lantern. A game about arriving somewhere, not about winning."
        ),
        "min_requirements": "4 GB RAM, integrated graphics, 2 GB free",
        "genres": ["Adventure", "Indie"],
        "price": 180_000,
        "suggested": 220_000,
        "colours": ((251, 191, 36), (120, 53, 15)),
        "commons": "Boats with lanterns on the Thu Bon river IMG 3864.jpg",
    },
    {
        "title": "Vault of Echoes",
        "description": (
            "A dungeon that remembers one more of its rooms every time you die in it."
        ),
        "min_requirements": "8 GB RAM, GTX 1660, 14 GB free",
        "genres": ["Roguelike", "Action"],
        "price": 620_000,
        "suggested": 680_000,
        "colours": ((168, 85, 247), (76, 29, 149)),
        "commons": "Antelope Canyon.jpg",
    },
    {
        "title": "Starforge Tactics",
        "description": (
            "You build a fleet that makes its own decisions. All you write is the doctrine it "
            "fights by."
        ),
        "min_requirements": "16 GB RAM, RTX 2060, 25 GB free",
        "genres": ["Strategy", "Sci-fi"],
        "price": 1_100_000,
        "suggested": 1_180_000,
        "colours": ((14, 165, 233), (2, 44, 84)),
        "commons": "Carina Nebula.jpg",
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
    body, content_type, filename = cover_bytes(spec)
    art = upload(
        "/media/v1/media",
        token=developer,
        filename=filename,
        content=body,
        content_type=content_type,
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
        ("POST", f"/catalog/v1/games/{game_id}/price/reject", {"amount_minor": spec["price"]}, developer),
        ("POST", f"/catalog/v1/games/{game_id}/publish", None, support),
    ]
    for method, path, payload, token in steps:
        call(method, path, token=token, body=payload)
    return game_id


def replace_cover(game: dict, spec: dict, developer: str) -> None:
    """Swap the teaser on an already-published game for a real photograph.

    The seeder leaves published titles alone, so a second run would otherwise keep the
    original gradient forever. A published game can still have its media replaced.
    """
    body, content_type, filename = cover_bytes(spec)
    art = upload(
        "/media/v1/media",
        token=developer,
        filename=filename,
        content=body,
        content_type=content_type,
        fields={"kind": "IMAGE", "reference_id": game["id"]},
    )
    for item in game.get("media") or []:
        if item.get("kind") == "TEASER":
            call("DELETE", f"/catalog/v1/games/{game['id']}/media/{item['id']}", token=developer)
    call(
        "POST",
        f"/catalog/v1/games/{game['id']}/media",
        token=developer,
        body={"kind": "TEASER", "media_ref": art["url"]},
    )


def multipart_fields(path: str, *, token: str, fields: dict[str, str]) -> dict:
    """A multipart POST with no file. community-service takes posts this way only."""
    boundary = f"----arcadia{uuid.uuid4().hex}"
    payload = b"".join(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        for name, value in fields.items()
    ) + f"--{boundary}--\r\n".encode()

    request = urllib.request.Request(f"{API}{path}", data=payload, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ApiError("POST", path, error.code, error.read()[:300].decode(errors="replace")) from None


REVIEWS = {
    "Neon Drift": "Thirty hours in and I still take the coast route badly. That is the compliment.",
    "Deep Signal": "I turned the torch off once to save battery and did not do it again.",
    "Paper Kingdoms": "Folded a peninsula I could not defend and the map tore exactly where I knew it would.",
    "Vault of Echoes": "Died in the same doorway four times. The fifth time the doorway was gone.",
    "Hollow Signal": "I sat with the headphones on after the credits. There is nothing else to say.",
}
POSTS = {
    "Neon Drift": (
        "Anyone else running this on a laptop? Dropping shadows to medium got me a steady 60 "
        "and I genuinely cannot see the difference at speed."
    ),
    "Lantern Way": (
        "Finished it in one sitting and then sat there. If you have an hour and a quiet room, "
        "this is the hour."
    ),
    "Ironworks": (
        "The spring flood ate my entire smelter line and I am not even angry. I should have "
        "read the contour map."
    ),
}
COLLECTOR_REVIEWS = {
    "Garden of Forking Paths": "Solved the greenhouse three ways. The fourth is still laughing at me.",
    "Starforge Tactics": "Wrote a doctrine that refused to engage. The fleet was right. I was not.",
}
COLLECTOR_POSTS = {
    "Garden of Forking Paths": (
        "Has anyone mapped the west wing without looking? I keep blinking and the hedges have moved."
    ),
}


def fund_if_needed(token: str, support_token: str, tag: str) -> None:
    balance = call("GET", "/wallet/v1/wallets/me", token=token)
    if int(balance["balance"]["amount_minor"]) >= 1_000_000:  # type: ignore[index]
        return
    issued = call(
        "POST",
        "/wallet/v1/gift-cards",
        token=support_token,
        body={
            "value": {"amount_minor": "9000000", "currency": "IRR"},
            "quantity": 1,
            "note": f"demo seed {tag}",
        },
        headers={"Idempotency-Key": f"seed-{tag}-{uuid.uuid4().hex}"},
    )
    code = issued["gift_cards"][0]["code"]  # type: ignore[index]
    call(
        "POST",
        "/wallet/v1/wallets/me/gift-cards/redeem",
        token=token,
        body={"code": code},
        headers={"Idempotency-Key": f"seed-redeem-{tag}-{uuid.uuid4().hex}"},
    )
    print(f"  funded {tag} with a gift card")


def buy_titles(token: str, player_id: str, titles: dict[str, str], by_title: dict, title_of: dict) -> set[str]:
    """Buy each title that is not already owned. Returns the owned set afterwards."""

    def owned_titles() -> set[str]:
        library = call("GET", "/catalog/v1/library", token=token)
        return {
            title_of[entry["game_id"]]  # type: ignore[index]
            for entry in library["items"]  # type: ignore[index,union-attr]
            if entry["game_id"] in title_of
        }

    owned = owned_titles()
    for title in titles:
        game = by_title.get(title)
        if game is None or title in owned:
            continue
        call(
            "POST",
            "/orders/v1/orders",
            token=token,
            body={"game_id": game["id"]},
            headers={"Idempotency-Key": f"seed-order-{player_id}-{game['id']}"},
        )
        print(f"  {player_id[:8]} bought {title}")

    for _ in range(10):
        owned = owned_titles()
        if all(title in owned for title in titles if title in by_title):
            break
        time.sleep(1.5)
    return owned


def leave_reviews(token: str, player_id: str, texts: dict[str, str], owned: set[str], by_title: dict) -> None:
    for title, text in texts.items():
        game = by_title.get(title)
        if game is None or title not in owned:
            continue
        existing = call("GET", f"/reviews/api/reviews/game/{game['id']}")
        if any(review["author_id"] == player_id for review in existing["reviews"]):  # type: ignore[index]
            continue
        call(
            "POST",
            "/reviews/api/reviews/",
            token=token,
            body={"game_id": game["id"], "text": text, "sentiment": "LIKE"},
        )
        print(f"  {player_id[:8]} reviewed {title}")


def leave_posts(token: str, texts: dict[str, str], by_title: dict) -> None:
    for title, text in texts.items():
        game = by_title.get(title)
        if game is None:
            continue
        feed = call("GET", f"/community/v1/games/{game['id']}/feed")
        bodies = [item.get("body") for item in feed.get("items", [])]  # type: ignore[union-attr]
        if text in bodies:
            continue
        multipart_fields(
            "/community/v1/posts/multipart",
            token=token,
            fields={"game_id": game["id"], "body": text, "spoiler": "false"},
        )
        print(f"  posted about {title}")


def add_activity(tokens: dict[str, str], ids: dict[str, str], extra: dict[str, tuple[str, str]]) -> dict:
    """Money, purchases, reviews and posts across more than one player.

    A storefront with games but no activity still demos as a shell. Each player buys a
    different slice of the catalogue so libraries, profiles and the community feed are
    not copies of each other.
    """
    catalogue = call("GET", "/catalog/v1/games?limit=100")
    by_title = {g["title"]: g for g in catalogue["items"]}  # type: ignore[index,union-attr]
    title_of = {g["id"]: g["title"] for g in catalogue["items"]}  # type: ignore[index,union-attr]

    fund_if_needed(tokens["BASIC_USER"], tokens["SUPPORT"], "player")
    owned = buy_titles(tokens["BASIC_USER"], ids["BASIC_USER"], REVIEWS, by_title, title_of)
    leave_reviews(tokens["BASIC_USER"], ids["BASIC_USER"], REVIEWS, owned, by_title)
    leave_posts(tokens["BASIC_USER"], POSTS, by_title)

    for tag, (token, user_id) in extra.items():
        fund_if_needed(token, tokens["SUPPORT"], tag)
        texts = COLLECTOR_REVIEWS if tag == "collector" else {}
        posts = COLLECTOR_POSTS if tag == "collector" else {}
        wanted = texts or {"Lantern Way": "", "Ironworks": ""}
        owned_extra = buy_titles(token, user_id, wanted, by_title, title_of)
        if texts:
            leave_reviews(token, user_id, texts, owned_extra, by_title)
        if posts:
            leave_posts(token, posts, by_title)

    return by_title


FESTIVAL_NAME = "Winter Arcade"
ITEM_TITLE = "Chrome Chassis"


def add_festival(admin_token: str, by_title: dict) -> None:
    """A running festival with games on it.

    The festivals screen is one of the more interesting things to show and it renders as an
    empty list until somebody creates one.
    """
    existing = call("GET", "/festivals/v1/festivals")
    items = existing.get("items", existing) if isinstance(existing, dict) else existing
    if any(f["name"] == FESTIVAL_NAME for f in items):  # type: ignore[union-attr]
        return

    now = datetime.now(UTC)
    festival = call(
        "POST",
        "/festivals/v1/festivals",
        token=admin_token,
        body={
            "name": FESTIVAL_NAME,
            "description": "Two weeks of discounts across the catalogue.",
            "starts_at": (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "ends_at": (now + timedelta(days=14)).isoformat().replace("+00:00", "Z"),
        },
    )
    festival_id = festival["id"]  # type: ignore[index]

    for title in ("Neon Drift", "Paper Kingdoms", "Ironworks"):
        game = by_title.get(title)
        if game:
            call(
                "POST",
                f"/festivals/v1/festivals/{festival_id}/games",
                token=admin_token,
                body={"game_id": game["id"]},
            )

    # Started explicitly rather than waiting for its own start time to arrive: a festival
    # that begins in a minute is not something to demonstrate a minute from now.
    call("POST", f"/festivals/v1/festivals/{festival_id}/start", token=admin_token)
    print(f"  opened the {FESTIVAL_NAME} festival with 3 games")


def add_marketplace(tokens: dict[str, str], ids: dict[str, str], by_title: dict) -> None:
    """A tradeable item, holdings, and a book with something in it.

    Item trading is requirement 1.6 and the screen is unreadable with nothing on it — an
    empty order book looks identical to a broken one. This leaves a resting order on each
    side and one executed trade, so the page shows a real market.
    """
    listing = call("GET", "/marketplace/v1/items?limit=100")
    items = listing.get("items", listing) if isinstance(listing, dict) else listing
    titles = {item["title"] for item in items}  # type: ignore[union-attr]

    catalogue = call("GET", "/catalog/v1/games?limit=100")
    by_title = {g["title"]: g for g in catalogue["items"]}  # type: ignore[index,union-attr]

    if ITEM_TITLE not in titles:
        game = by_title.get("Neon Drift")
        if game is not None:
            art = next((m["media_ref"] for m in game["media"]), "")
            item = call(
                "POST",
                "/marketplace/v1/items",
                token=tokens["DEVELOPER"],
                body={
                    "game_id": game["id"],
                    "title": ITEM_TITLE,
                    "description": "A rare chassis skin. Drops once per season.",
                    "image_url": art,
                    "buy_value": "120000",
                    "sell_value": "90000",
                },
            )
            item_id = item["id"]  # type: ignore[index]
            call(
                "POST",
                f"/marketplace/v1/items/{item_id}/grant",
                token=tokens["SUPPORT"],
                body={"user_ids": [ids["DEVELOPER"], ids["BASIC_USER"]]},
            )
            call("POST", "/marketplace/v1/orders", token=tokens["DEVELOPER"],
                 body={"item_id": item_id, "side": "SELL", "price": "100000"})
            call("POST", "/marketplace/v1/orders", token=tokens["BASIC_USER"],
                 body={"item_id": item_id, "side": "BUY", "price": "100000"})
            call("POST", "/marketplace/v1/admin/matching/run", token=tokens["SUPPORT"])
            call("POST", "/marketplace/v1/orders", token=tokens["BASIC_USER"],
                 body={"item_id": item_id, "side": "BUY", "price": "85000"})
            call("POST", "/marketplace/v1/orders", token=tokens["DEVELOPER"],
                 body={"item_id": item_id, "side": "SELL", "price": "115000"})
            print(f"  listed {ITEM_TITLE} with a live order book")
            titles.add(ITEM_TITLE)

    second = "Echo Shard"
    if second in titles:
        return
    vault = by_title.get("Vault of Echoes")
    collector = ids.get("collector")
    if vault is None or collector is None:
        print(f"  ! skip {second}: vault={vault is not None} collector={collector is not None}")
        return
    art = next((m["media_ref"] for m in vault["media"]), "")
    shard = call(
        "POST",
        "/marketplace/v1/items",
        token=tokens["DEVELOPER"],
        body={
            "game_id": vault["id"],
            "title": second,
            "description": "A fragment of a room the dungeon has not rebuilt yet.",
            "image_url": art,
            "buy_value": "80000",
            "sell_value": "55000",
        },
    )
    shard_id = shard["id"]  # type: ignore[index]
    call(
        "POST",
        f"/marketplace/v1/items/{shard_id}/grant",
        token=tokens["SUPPORT"],
        body={"user_ids": [ids["BASIC_USER"], collector]},
    )
    print(f"  listed {second} and granted it to two players")


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
        ("collector@arcadia.example", "Jules Voss", "BASIC_USER"),
        ("rival@arcadia.example", "Mira Chen", "BASIC_USER"),
    ]
    tokens: dict[str, str] = {}
    ids: dict[str, str] = {}
    extras: dict[str, tuple[str, str]] = {}
    for email, name, role in accounts:
        user_id, token = ensure_account(admin_token, email, name, role)
        tag = email.split("@")[0]
        if role in tokens and role == "BASIC_USER":
            extras[tag] = (token, user_id)
        else:
            tokens[role] = token
            ids[role] = user_id
        ids[tag] = user_id
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

    catalogue = call("GET", "/catalog/v1/games?limit=100")
    by_title = {g["title"]: g for g in catalogue["items"]}  # type: ignore[index,union-attr]
    specs = {spec["title"]: spec for spec in GAMES}
    for title, game in by_title.items():
        spec = specs.get(title)
        if spec is None:
            continue
        try:
            replace_cover(game, spec, tokens["DEVELOPER"])
            print(f"  cover {title}")
        except ApiError as error:
            print(f"  ! could not replace the cover for {title}: {error}")

    by_title = add_activity(tokens, ids, extras)

    # The storefront is the demo; these are extras on top of it. One service being unwell
    # should leave the rest of the content seeded rather than abandoning the run — and it
    # says which one and why, because a seeder that swallows a failure is worse than one
    # that stops.
    for name, seed in (
        ("festival", lambda: add_festival(admin_token, by_title)),
        ("marketplace", lambda: add_marketplace(tokens, ids, by_title)),
    ):
        try:
            seed()
        except ApiError as error:
            print(f"  ! could not seed the {name}: {error}")

    total = len(published_titles())
    print(f"\n{added} published, {total} on the storefront")
    print(f"demo accounts all use the password: {DEMO_PASSWORD}")
    print("  developer@arcadia.example  (DEVELOPER)")
    print("  support@arcadia.example    (SUPPORT)")
    print("  player@arcadia.example     (Nadia — library + reviews)")
    print("  collector@arcadia.example  (Jules — other titles + market items)")
    print("  rival@arcadia.example      (Mira — a second library)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as error:
        print(f"\nfailed: {error}", file=sys.stderr)
        sys.exit(1)
