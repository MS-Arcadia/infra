# End-to-end tests

75 checks against the **running** platform. Five services, real Kafka, real Postgres, real
files on disk.

**Run manually. Deliberately not in CI** — see [Why not in CI](#why-not-in-ci).

```bash
cd infra
make up && make wait          # the platform has to be running
make e2e                      # ~20 seconds
```

---

## Why this exists

The services have 389 unit tests between them. Those tests were all green when the platform
was first started with Docker, and **nine bugs** surfaced in the first hour. Not one of them
was about logic — every one was about how a service meets the world:

| | |
|---|---|
| Two consumers in one Kafka group on different topics | Partitions assigned to the member that ignored them. Every purchase hung in `PENDING`, no error in any log. |
| Nobody created `wallet-commands` | The consumer thought the producer owned it and vice versa. Auto-creation is off. |
| Every read path had no database session | 500 on the call the order service makes before every purchase. |
| `pydantic-settings` JSON-decodes list env vars | `KAFKA_BROKERS=kafka:9092` crash-looped the container. |
| The Go services publish with snappy | The Python consumers had no codec and their loops died silently. |
| `JWT_ALGORITHM=HS256` came back lowercased | The verifier refused it and the service would not boot. |
| Issuer and audience unverified in three of five services | A token from a foreign issuer would have been accepted. |
| `PLATFORM_USER_ID` was not a valid UUID | Would have failed on the platform credit — after the buyer was debited. |
| The replay check ran after the catalog call | A retry of a successful purchase got `409 GAME_ALREADY_OWNED`. |

A unit test cannot find any of those. That is what this suite is for.

---

## What it covers

| File | |
|---|---|
| `test_01_publishing.py` | Requirement 1.3 — register, submit, review, appeal, price, publish |
| `test_02_purchase.py` | Requirement 1.4 and §6.1 — the saga, the 70/30 split, idempotency |
| `test_03_refund.py` | §6.2 — the twelve-hour window, the reversals, re-purchase |
| `test_04_gift.py` | Gifts, the 2% message fee, and that a gift is not refundable |
| `test_05_compensation.py` | The compensation path, reached with a real race |
| `test_06_media.py` | Uploads, disguised files, signed download URLs |
| `test_99_platform_health.py` | Invariants: DLQs empty, outboxes drained, the ledger balances |

The numbering is load-bearing: pytest runs files in name order, and the suite tells one story
— a game is published, then bought, then refunded, then gifted.

### The compensation test is the one worth reading

A buyer whose money moved and whose game did not is the worst thing this platform can do, and
compensation is what prevents it. It cannot be triggered by buying something twice — the
order service's pre-flight check correctly refuses that before any money moves.

So `test_05` fires **two concurrent gifts to the same fresh recipient**. Both pass the
pre-flight, because neither has granted yet. Both debit the buyer. One grant then wins and
the other is refused with `GAME_ALREADY_OWNED`, which reaches the saga as an event rather
than an exception and triggers the refund.

The assertion is that the buyer was charged **once, for one game**, and that the recipient
owns it exactly once.

If the pre-flight happens to catch the second order, the test skips rather than fails — no
money moved, which is a better outcome than compensating.

### `test_99` reads the databases directly

Normally the wrong thing for a test to do. It is right for one narrow purpose: proving
invariants that no API exposes.

* Every dead-letter topic is empty.
* Every outbox drained, and nothing exhausted its retries.
* **Every balance still equals the sum of its ledger.** The wallet's central invariant.
* No order is stuck mid-saga, and no saga was abandoned.
* No entitlement is duplicated, and no media row lacks its bytes.

---

## Repeatable without resetting

Every run generates fresh user ids, so `pytest` twice in a row passes twice in a row. That
matters more than it sounds: a suite that needs `make nuke` first is a suite people stop
running.

Balance assertions are **deltas** against a snapshot taken inside the fixture, never absolute
totals, for the same reason.

```bash
make e2e && make e2e && make e2e     # all three pass
```

`make nuke` is only needed if you want a clean database for other reasons.

---

## Two things it deliberately does not do

**It does not publish `UserRegistered` to create wallets.** In production a wallet is created
by the wallet service's consumer, driven by Auth. There is no Auth service in this stack, and
`GET /v1/wallets/me` provisions on first access — which exists precisely so a user is never
blocked waiting for an event.

The first version did publish the event, through `kafka-console-producer.sh`. That command
**hangs** under `docker exec`: it reads stdin until EOF and never sees it. Five minutes of
runtime for something a 40ms HTTP call does. The consumer is covered by the wallet service's
own tests; this suite is for the flows that cross services.

**It does not test the payment gateway.** A bank top-up needs a browser to visit the sandbox
gateway's authorisation page. The payment service's own suite covers that with a scriptable
fake bank.

---

## Why not in CI

It needs five services, Postgres and Kafka running together. That job does not belong in any
one service's repository, because no service owns "all the services".

It could live here, in the infra pipeline, at roughly five minutes per run — building five
images and starting eight containers. That has not been done because a five-minute job on
every infra PR is a cost worth choosing deliberately rather than inheriting.

**If you want it in CI**, the job is:

```yaml
smoke:
  needs: validate
  steps:
    - # check out all five service repositories
    - run: make images && make up && make wait
    - run: pip install -r test/e2e/requirements.txt && pytest test/e2e -q
```

Every one of the nine bugs above would have been caught by it.

---

## Running less than all of it

```bash
pytest test/e2e -q                                  # everything, ~20s
pytest test/e2e/test_02_purchase.py -v              # one flow
pytest test/e2e/test_99_platform_health.py -v       # just the invariants, ~5s
pytest test/e2e -m "not slow"                       # skip anything that waits on the saga
pytest test/e2e --durations=10                      # find what is slow
```

`test_99` on its own is the quickest useful check after an incident: it says whether anything
is dead-lettered, stuck, or out of balance, without changing any state.

---

## When it fails

The suite exits early with a clear message if the platform is not ready:

```
the platform is not ready:
  media-service: 503 {'status': 'DOWN', 'checks': {'storage': {'status': 'DOWN', ...}}}

Start it with:  cd infra && make up && make wait
```

For a genuine failure, in order:

```bash
docker logs arcadia-order --tail 50          # or whichever service
make ps                                      # is everything still healthy?
pytest test/e2e/test_99_platform_health.py -v # is anything dead-lettered or out of balance?
```

An order stuck in `PENDING` is almost always Kafka: check the consumer groups, which is
where the group-per-topic bug was finally visible.

```bash
docker exec arcadia-kafka kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --describe --all-groups
```

A `CURRENT-OFFSET` of `-` on a partition with a non-zero `LOG-END-OFFSET` means messages are
arriving and nothing is processing them.

---

## Requirements

```bash
pip install -r requirements.txt   # pytest and pyjwt, nothing else
```

`arcadia.py` is built on `urllib` rather than `httpx` on purpose: the reason to run these
tests is to check the platform, and a dependency that fails to install is a distraction from
that. The only two that are unavoidable are pytest itself and a JWT library, because every
endpoint requires a signed token.

`docker` must be on the PATH — `test_99` reads the databases and Kafka through
`docker exec`.
