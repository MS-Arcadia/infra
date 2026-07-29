"""The gateway, against the running platform.

Every other file here talks to a service directly. This one talks to :8090 and compares,
because the questions worth asking about a reverse proxy are the ones a unit test cannot
answer: does the published routing table match where requests actually land, does a real
response survive the hop unchanged, and does the token contract hold on both sides of it.

The two design decisions this file exists to protect:

  * The gateway does no authorisation. A valid token for the wrong role passes the edge
    and is refused by the service, with the service's reason.
  * The gateway does not require a token. A request without one is forwarded, not
    refused, so there is no list of public routes to fall out of date.

Both are easy to "improve" into a role table and a public-route list at the edge. Both
would then be a second copy of a rule that lives somewhere else — which is how this
platform lost a notification once already.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from arcadia import CATALOG, GATEWAY, WALLET, call, new_id, token

# Prefix at the gateway -> the same resource, reached directly. Transcribed from the
# gateway's routes.go, and test_the_published_routing_table_is_not_a_fiction proves the
# gateway agrees.
ROUTES = {
    "/auth": "auth-profile-service",
    "/catalog": "catalog-service",
    "/orders": "order-service",
    "/wallet": "wallet-service",
    "/payment": "payment-service",
    "/media": "media-service",
    "/notifications": "notification-service",
    "/marketplace": "marketplace-service",
}


def gw(method: str, path: str, **kwargs):
    return call(method, GATEWAY + path, **kwargs)


# --------------------------------------------------------------------------- routing


def test_the_published_routing_table_is_not_a_fiction():
    """`GET /` is the first thing anybody debugging this reads. It has to be true."""
    response = call("GET", GATEWAY + "/")
    assert response.status == 200

    published = {route["prefix"]: route["service"] for route in response.body["routes"]}
    assert published == ROUTES


@pytest.mark.parametrize("prefix", sorted(ROUTES))
def test_every_prefix_reaches_a_service_rather_than_the_gateway(prefix):
    """A prefix that is routed but unreachable answers 503; one that is not routed at all
    answers 404 NO_ROUTE. Both are the gateway talking. Anything else means a service
    answered, which is the whole job.
    """
    response = gw("GET", f"{prefix}/v1", user=new_id())

    reason = response.body.get("reason") if isinstance(response.body, dict) else None
    assert reason != "NO_ROUTE", f"{prefix} is published in the table but not routed"
    assert response.status != 503, f"{ROUTES[prefix]} is not reachable from the gateway"


def test_a_response_through_the_gateway_is_the_response_the_service_gave():
    """No rewriting, no re-encoding, no truncation."""
    through = call("GET", GATEWAY + "/catalog/v1/games?limit=3")
    direct = call("GET", CATALOG + "/v1/games?limit=3")

    assert through.status == direct.status == 200
    assert through.body == direct.body


def test_the_query_string_survives_the_hop():
    """`Rewrite` sets the outbound path outright, which is exactly the place a query
    string gets dropped by accident. It would not show up as an error — just as a page
    size that stopped working.
    """
    one = call("GET", GATEWAY + "/catalog/v1/games?limit=1")
    three = call("GET", GATEWAY + "/catalog/v1/games?limit=3")

    assert one.body["limit"] == 1
    assert three.body["limit"] == 3
    assert len(one.body["items"]) == 1
    assert len(three.body["items"]) == 3


def test_a_request_body_survives_the_hop():
    """A POST whose body arrived empty would look like a validation bug in the service."""
    developer = new_id()
    response = gw(
        "POST",
        "/catalog/v1/games",
        user=developer,
        role="DEVELOPER",
        body={"title": "Gateway Body Probe", "description": "d", "min_requirements": ""},
    )

    assert response.status == 201, response
    assert response.body["title"] == "Gateway Body Probe"


def test_an_unrouted_prefix_is_a_problem_document_naming_the_reason():
    response = gw("GET", "/festival/v1/festivals")

    assert response.status == 404
    assert response.body["reason"] == "NO_ROUTE"
    # Not a bare 404: the caller has to be able to tell "no such service" from "no such
    # resource in that service", and the two are different people's problem.
    assert "/festival/v1/festivals" in response.body["detail"]


# ------------------------------------------------------------------- the token contract


def test_a_token_the_gateway_accepts_is_a_token_the_services_accept():
    """One claim contract, not two. If the gateway verified tokens the services reject,
    every call would 401 *behind* a gateway that said they were fine.
    """
    user = new_id()

    for label, path in [
        ("wallet", "/wallet/v1/wallets/me"),
        ("order", "/orders/v1/orders"),
        ("notification", "/notifications/v1/notifications"),
        ("catalog", "/catalog/v1/library"),
    ]:
        response = gw("GET", path, user=user)
        assert response.status == 200, f"{label} rejected a token the gateway accepted: {response}"


@pytest.mark.parametrize(
    ("label", "bearer"),
    [
        ("not a jwt at all", "banana"),
        ("a tampered signature", token(new_id())[:-4] + "aaaa"),
        ("an empty bearer", " "),
    ],
)
def test_a_malformed_token_never_reaches_a_service(label, bearer):
    """Refused at the edge, so a service never pays for a signature verification it was
    always going to fail. The reason comes from the gateway, not from wallet.
    """
    response = gw("GET", "/wallet/v1/wallets/me", bearer=bearer)

    assert response.status == 401, label
    assert response.body["reason"] in {"TOKEN_INVALID", "TOKEN_MISSING"}, response


def test_a_refresh_token_cannot_be_used_to_call_the_api():
    """A whitelist on `typ`, not a blacklist: anything that is not an access token is
    refused, so a new token type added tomorrow is refused by default rather than
    accepted by omission.
    """
    import jwt as pyjwt
    from datetime import UTC, datetime, timedelta

    from arcadia import AUDIENCE, ISSUER, JWT_SECRET

    refresh = pyjwt.encode(
        {
            "sub": new_id(),
            "role": "BASIC_USER",
            "typ": "refresh",
            "scopes": [],
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        JWT_SECRET,
        algorithm="HS256",
    )

    response = gw("GET", "/wallet/v1/wallets/me", bearer=refresh)
    assert response.status == 401
    # Not the generic TOKEN_INVALID: a refresh token presented to the API is a client
    # bug with an obvious fix, and saying so saves somebody an hour.
    assert response.body["reason"] == "REFRESH_TOKEN_USED", response


# ------------------------------------------------------ what the gateway deliberately omits


def test_the_gateway_does_no_authorisation():
    """A perfectly valid token for the wrong role reaches the service, and the *service*
    refuses it — naming the role it wanted.

    A role table at the edge would answer this faster and would be a second copy of a
    rule catalog already owns. When they drifted, the edge would be wrong.
    """
    user = new_id()

    refused = gw("GET", "/catalog/v1/games/mine", user=user, role="BASIC_USER")
    assert refused.status == 403
    assert refused.body["reason"] == "ROLE_REQUIRED", refused
    assert "DEVELOPER" in refused.body["detail"]

    allowed = gw("GET", "/catalog/v1/games/mine", user=user, role="DEVELOPER")
    assert allowed.status == 200


def test_a_request_with_no_token_is_forwarded_rather_than_refused():
    """The decision that removes the public-route list.

    The gateway verifies a token if one is present and passes its absence through. Proven
    by comparing bodies: an unauthenticated call through the gateway gets byte-for-byte
    what wallet itself says, which it could only do by having reached wallet.
    """
    through = call("GET", GATEWAY + "/wallet/v1/wallets/me")
    direct = call("GET", WALLET + "/v1/wallets/me")

    assert through.status == direct.status == 401
    assert through.body["detail"] == direct.body["detail"]
    assert through.body["title"] == direct.body["title"]
    # And it is wallet's refusal, not the gateway's: every refusal the gateway makes
    # itself carries a `reason` naming the check that failed.
    assert "reason" not in through.body, f"the gateway refused this instead of forwarding it: {through}"


def test_a_public_endpoint_needs_no_token_through_the_gateway():
    """The storefront's game list is public, and stayed public behind the gateway."""
    response = call("GET", GATEWAY + "/catalog/v1/games?limit=1")

    assert response.status == 200
    assert "items" in response.body


# ------------------------------------------------------------------------- correlation


def test_the_correlation_id_reaches_the_service_and_comes_back():
    """One id through eight processes. The service echoes it into its own error body,
    which is what makes "show me everything for this call" answerable.
    """
    supplied = "019fabcd-0000-7000-8000-aaaaaaaaaaaa"

    request = urllib.request.Request(GATEWAY + "/wallet/v1/wallets/me")
    request.add_header("X-Correlation-Id", supplied)
    try:
        urllib.request.urlopen(request, timeout=30)
        pytest.fail("an unauthenticated wallet call should be 401")
    except urllib.error.HTTPError as exc:
        returned = exc.headers.get("X-Correlation-Id")
        body = json.loads(exc.read())

    assert returned == supplied, "a client-supplied id must be honoured, not replaced"
    assert body["trace_id"] == supplied, "the id must reach the service, not stop at the edge"


def test_a_proxied_response_carries_exactly_one_correlation_id():
    """The services set this header themselves, and a reverse proxy appends rather than
    replaces. Two values means a client reads whichever its HTTP library returns first.
    """
    request = urllib.request.Request(GATEWAY + "/catalog/v1/games?limit=1")
    with urllib.request.urlopen(request, timeout=30) as response:
        ids = response.headers.get_all("X-Correlation-Id") or []

    assert len(ids) == 1, f"expected one correlation id, got {ids}"


def test_every_response_carries_a_correlation_id_including_the_failures():
    """An error a client cannot quote an id for is an error nobody can find in eight
    services' logs.
    """
    for path in ("/catalog/v1/games?limit=1", "/nowhere/at/all", "/wallet/v1/wallets/me"):
        response = call("GET", GATEWAY + path)
        assert response.headers.get("x-correlation-id"), f"no correlation id on {path}"


# ------------------------------------------------------------------------- operational


def test_the_operational_routes_belong_to_the_gateway():
    """Registered on the mux directly, so no upstream can claim them — otherwise an
    orchestrator would be reading somebody else's health.
    """
    for path in ("/livez", "/readyz"):
        response = call("GET", GATEWAY + path)
        assert response.status == 200
        assert response.body["service"] == "api-gateway", f"{path} was proxied"


def test_readiness_reports_every_upstream_and_none_of_them_are_critical():
    """Non-critical on purpose. A gateway that called itself unready because one of seven
    services was down would take the whole platform offline over a single failure.
    """
    response = call("GET", GATEWAY + "/readyz")

    assert response.status == 200
    checks = {check["name"]: check for check in response.body["checks"]}
    assert set(checks) == set(ROUTES.values())
    for name, check in checks.items():
        assert check["critical"] is False, f"{name} must not be able to take the gateway down"


def test_metrics_are_exposed_and_labelled_by_prefix():
    """By prefix, not by path: `/catalog/v1/games/{id}` as a label would mint a time
    series per game id and eventually cost more than the platform it measures.
    """
    call("GET", GATEWAY + "/catalog/v1/games?limit=1")

    request = urllib.request.Request(GATEWAY + "/metrics")
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode()

    assert "gateway_requests_total" in body
    assert 'prefix="/catalog"' in body
    # If a raw path ever appears as a label, that is the cardinality bug arriving.
    assert "/v1/games?" not in body
