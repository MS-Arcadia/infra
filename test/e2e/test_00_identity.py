"""Identity: the auth service, and whether the rest of the platform accepts what it issues.

Numbered 00 because it comes before everything. Every other file in this suite signs its own
tokens — which is right for them, they are testing the platform and not the issuer — but that means
nothing else here would notice if the real issuer stopped producing acceptable ones. It did not
produce acceptable ones at all: every token it minted was rejected by all five services, because it
carried no `iss` and no `aud`.

The two checks worth reading are the last two. `test_a_new_account_gets_a_wallet_from_the_event`
proves `arcadia.auth.v1.UserRegistered` crosses Kafka and is understood — the wallet had been
subscribed to that exact name since before this service existed, and never received anything it
could parse. `test_a_purchase_shows_up_in_the_profile_library` closes the loop in the other
direction: the catalog's ownership event reaching a read-model in a different service.
"""

from __future__ import annotations

import time
import uuid

import arcadia as a
import jwt
import pytest

AUTH = a.AUTH
PASSWORD = "SuperSecret123!"


# --- helpers -------------------------------------------------------------


def register(display_name: str = "A Player") -> tuple[str, str]:
    """Register, returning (user_id, email). The account is PENDING."""
    email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
    response = a.call(
        "POST",
        f"{AUTH}/v1/auth/register",
        body={"email": email, "password": PASSWORD, "display_name": display_name},
    )
    assert response.status == 201, response
    return response.body["user_id"], email


def approve(user_id: str) -> None:
    """Support approves a registration. Requirement 1.1 puts every account through this."""
    response = a.call(
        "POST",
        f"{AUTH}/v1/registrations/{user_id}/decide",
        user=a.new_id(),
        role="SUPPORT",
        body={"approve": True},
    )
    assert response.status in (200, 204), response


def grant(user_id: str, role: str) -> None:
    response = a.call(
        "POST",
        f"{AUTH}/v1/admin/users/{user_id}/grant-role",
        user=a.new_id(),
        role="ADMIN",
        # `new_role`, not `role` — the field says what it is changing to.
        body={"new_role": role},
    )
    assert response.status in (200, 204), response


def login(email: str) -> dict:
    response = a.call("POST", f"{AUTH}/v1/auth/login", body={"email": email, "password": PASSWORD})
    assert response.status == 200, response
    return response.body


def real_user(role: str | None = None) -> tuple[str, str]:
    """A user who actually registered, was approved, and logged in.

    Returns (user_id, access_token) — a token minted by the auth service rather than by this
    suite, which is the whole point of this file.
    """
    user_id, email = register()
    approve(user_id)
    if role:
        grant(user_id, role)
    return user_id, login(email)["access_token"]


# --- shared identities ---------------------------------------------------
#
# Session-scoped, because registering is rate-limited and because most of these tests want *a*
# real user rather than a brand-new one. The tests that genuinely need a fresh PENDING account
# — the state-machine ones — still register their own.


@pytest.fixture(scope="session")
def basic() -> tuple[str, str]:
    return real_user()


@pytest.fixture(scope="session")
def developer() -> tuple[str, str]:
    return real_user("DEVELOPER")


@pytest.fixture(scope="session")
def support() -> tuple[str, str]:
    return real_user("SUPPORT")


@pytest.fixture(scope="session")
def administrator() -> tuple[str, str]:
    return real_user("ADMIN")


@pytest.fixture(scope="session")
def refresh_token() -> str:
    """One refresh token, reused. It is long-lived by design, which is the whole reason it must not
    work as a credential — so a session-scoped one is if anything the harder test."""
    user_id, email = register()
    approve(user_id)
    return login(email)["refresh_token"]


# --- the account state machine ------------------------------------------


def test_a_new_account_starts_pending():
    """Requirement 1.1: the initial state is PENDING and Support decides.

    This was ACTIVE, which made the whole approve/reject flow unreachable — nobody can be approved
    when everybody already is — and failed five of the auth service's own tests.
    """
    user_id, _ = register()
    assert user_id
    stored = a.psql("auth", f"SELECT state FROM users WHERE id = '{user_id}'")
    assert stored.strip() == "PENDING"


def test_a_pending_account_cannot_log_in_and_is_told_why():
    """403 with a reason, not 401 "invalid email or password".

    This branch is only reachable with the *correct* password, so saying so leaks nothing to an
    attacker — and telling somebody waiting for Support that they have forgotten their password is
    the one message guaranteed to produce a support ticket.
    """
    _, email = register()
    response = a.call("POST", f"{AUTH}/v1/auth/login", body={"email": email, "password": PASSWORD})
    assert response.status == 403, response
    assert "Support" in str(response.body)


def test_a_wrong_password_still_says_nothing():
    """The other side of that distinction: a bad password must not reveal whether the account
    exists or what state it is in."""
    _, email = register()
    response = a.call("POST", f"{AUTH}/v1/auth/login", body={"email": email, "password": "wrong"})
    assert response.status == 401, response
    assert "Support" not in str(response.body)


def test_support_approves_and_then_the_account_works():
    user_id, email = register()
    approve(user_id)
    tokens = login(email)
    assert tokens["access_token"]
    assert tokens["refresh_token"]


def test_a_banned_account_cannot_log_in():
    user_id, email = register()
    approve(user_id)
    assert login(email)["access_token"]

    banned = a.call(
        "POST",
        f"{AUTH}/v1/admin/users/{user_id}/ban",
        user=a.new_id(),
        role="SUPPORT",
        body={"reason": "e2e"},
    )
    assert banned.status in (200, 204), banned

    response = a.call("POST", f"{AUTH}/v1/auth/login", body={"email": email, "password": PASSWORD})
    assert response.status == 403, response
    assert "banned" in str(response.body).lower()


# --- the token contract --------------------------------------------------


def test_the_access_token_carries_what_every_service_verifies(basic):
    """The claims are the contract. `iss` and `aud` were both absent, which is why every service
    answered 401 — and `typ` was spelled `type`, which our verifiers ignored."""
    _, token = basic
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["iss"] == a.ISSUER
    assert claims["aud"] == a.AUDIENCE
    assert claims["typ"] == "access"
    assert claims["role"] in ("BASIC_USER", "DEVELOPER", "SUPPORT", "ADMIN")
    assert "scopes" in claims


@pytest.mark.parametrize(
    ("service", "url"),
    [
        ("wallet", f"{a.WALLET}/v1/wallets/me"),
        ("payment", f"{a.PAYMENT}/v1/payments?page=1&page_size=5"),
        ("catalog", f"{a.CATALOG}/v1/library"),
        ("order", f"{a.ORDER}/v1/orders"),
        ("media", f"{a.MEDIA}/v1/media/usage"),
        ("notification", f"{a.NOTIFICATION}/v1/notifications"),
    ],
)
def test_every_service_accepts_a_token_the_auth_service_issued(service: str, url: str, basic):
    """Five services, five 401s before this was fixed — notification-service was written after and
    inherited the corrected verifier. Parameterised so the failure names which one, because they do
    not all verify identically."""
    _, token = basic
    response = a.call("GET", url, bearer=token)
    assert response.status == 200, f"{service} rejected a real access token: {response}"


@pytest.mark.parametrize(
    ("service", "url"),
    [
        ("wallet", f"{a.WALLET}/v1/wallets/me"),
        ("catalog", f"{a.CATALOG}/v1/library"),
        ("order", f"{a.ORDER}/v1/orders"),
        ("notification", f"{a.NOTIFICATION}/v1/notifications"),
    ],
)
def test_no_service_accepts_a_refresh_token(service: str, url: str, refresh_token: str):
    """A refresh token lives for days and exists only to be exchanged. Accepting one as a
    credential hands out a long-lived API key.

    This was live: the issuer wrote `type: refresh` while every verifier read `typ`, so the claim
    arrived empty and an absent one was treated as "access". A seven-day token carrying a full role
    worked on every endpoint on the platform.
    """
    response = a.call("GET", url, bearer=refresh_token)
    assert response.status == 401, f"{service} accepted a refresh token: {response}"


def test_a_refresh_token_still_refreshes(refresh_token: str):
    """The other half: making the check strict must not break the flow it protects."""
    response = a.call("POST", f"{AUTH}/v1/auth/refresh", body={"refresh_token": refresh_token})
    assert response.status == 200, response
    assert response.body["access_token"]


def test_a_granted_role_reaches_the_other_services(basic, developer):
    """A role is only useful if it travels. Creating a game is DEVELOPER-only in the catalog, so
    this asserts the claim arrived and was believed."""
    _, basic_token = basic
    refused = a.call(
        "POST",
        f"{a.CATALOG}/v1/games",
        bearer=basic_token, key=str(uuid.uuid4()),
        body={"title": "Should Not Exist", "description": "a basic user may not publish"},
    )
    assert refused.status == 403, refused

    _, dev_token = developer
    allowed = a.call(
        "POST",
        f"{a.CATALOG}/v1/games",
        bearer=dev_token, key=str(uuid.uuid4()),
        body={"title": "Auth Integration", "description": "created with a real developer token"},
    )
    assert allowed.status == 201, allowed


# --- events across the platform -----------------------------------------


def test_a_new_account_gets_a_wallet_from_the_event():
    """`arcadia.auth.v1.UserRegistered` crossing Kafka, understood at the far end.

    Deliberately touches nothing on the wallet's API: `GET /v1/wallets/me` provisions on first
    access, so calling it would prove nothing about the event. The first version of this check did
    exactly that and passed for the wrong reason.

    It also covers a boot-order bug. `user-events` was created by whoever produced to it, and the
    wallet subscribed at startup — so on a cold stack the wallet's client could subscribe to a topic
    that did not exist and never go back for it. Provisioning worked after a restart and not on a
    fresh `make up`.
    """
    user_id, _ = register()

    for _ in range(60):
        if user_id in a.psql("wallet", f"SELECT user_id FROM wallets WHERE user_id='{user_id}'"):
            return
        time.sleep(0.5)
    raise AssertionError(f"no wallet was provisioned for {user_id} from UserRegistered")


def test_a_new_account_gets_a_profile_from_the_event():
    """Auth and Profile share a deployment and still talk only through events, which is what keeps
    them separable. The row appearing proves the loop actually closes."""
    user_id, _ = register()

    for _ in range(40):
        if user_id in a.psql("auth", f"SELECT user_id FROM profiles WHERE user_id='{user_id}'"):
            return
        time.sleep(0.5)
    raise AssertionError(f"no profile row was projected for {user_id}")


@pytest.mark.slow
def test_a_purchase_shows_up_in_the_profile_library(developer, support, administrator, basic):
    """The whole platform in one assertion, using only tokens the auth service issued.

    A game is published, bought, and appears in the buyer's profile — which means the catalog's
    `OwnershipGranted` crossed `game-events` and was projected by a read-model in another service.
    That projector read `user_id` from the top level of the message; the catalog sends `owner_id`
    inside `payload`, so it had never once worked.
    """
    _, dev = developer
    _, reviewer = support
    _, admin = administrator
    buyer_id, buyer = basic

    created = a.call(
        "POST", f"{a.CATALOG}/v1/games", bearer=dev, key=str(uuid.uuid4()),
        body={"title": "Library Projection", "description": "bought with a real token"},
    )
    assert created.status == 201, created
    game_id = created.body["id"]

    steps = (
        ("POST", f"{a.CATALOG}/v1/games/{game_id}/versions", dev,
         {"version": "1.0.0", "file_ref": "build-1", "size_bytes": 1024}),
        ("POST", f"{a.CATALOG}/v1/games/{game_id}/submit", dev, None),
        ("POST", f"{a.CATALOG}/v1/games/{game_id}/review/start", reviewer, None),
        ("POST", f"{a.CATALOG}/v1/games/{game_id}/review/approve", reviewer, {"note": "ok"}),
        ("POST", f"{a.CATALOG}/v1/games/{game_id}/price", dev,
         {"amount_minor": 1_000_000, "currency": "IRR"}),
        ("POST", f"{a.CATALOG}/v1/games/{game_id}/publish", dev, None),
    )
    for method, url, token, body in steps:
        response = a.call(method, url, bearer=token, key=str(uuid.uuid4()), body=body)
        assert response.status in (200, 201), f"{url} -> {response}"

    funded = a.call(
        "POST", f"{a.WALLET}/v1/admin/wallets/{buyer_id}/adjust", bearer=admin, key=str(uuid.uuid4()),
        body={"direction": "CREDIT", "amount": {"amount_minor": "5000000", "currency": "IRR"},
              "reason": "seed for the identity test"},
    )
    assert funded.status == 200, funded

    placed = a.call(
        "POST", f"{a.ORDER}/v1/orders", bearer=buyer, key=str(uuid.uuid4()), body={"game_id": game_id}
    )
    assert placed.status == 202, placed
    order_id = placed.body["id"]

    for _ in range(60):
        order = a.call("GET", f"{a.ORDER}/v1/orders/{order_id}", bearer=buyer).body
        if order["state"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.4)
    assert order["state"] == "COMPLETED", order

    for _ in range(60):
        profile = a.call("GET", f"{AUTH}/v1/profile/{buyer_id}", bearer=buyer).body
        owned = [game.get("game_id") for game in (profile or {}).get("owned_games", [])]
        if game_id in owned:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"the game never reached the profile library; profile was {str(profile)[:300]}"
    )
