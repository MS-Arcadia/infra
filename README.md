# Arcadia — Infrastructure

Shared platform code, API contracts and deployment manifests for the [Arcadia](../PHASE01/README.md)
game distribution platform.

This repository holds what more than one service needs. Everything here exists because
duplicating it across fourteen services would guarantee fourteen slightly different
versions of it.

```
infra/
├── contracts/          protobuf API definitions (the source of truth for every service API)
├── platform/           the shared Go module: logging, money, outbox, auth, transports…
└── deploy/
    ├── compose/        the local platform
    ├── postgres/init/  per-service database and role provisioning
    ├── observability/  OTel collector, Prometheus, Loki, Tempo, Grafana
    └── k8s/            kustomize base and overlays
```

## Getting started

```bash
make up          # Postgres, Redis, Kafka, wallet, payment
make up-full     # the above plus Grafana, Prometheus, Loki, Tempo and a Kafka UI
make logs
make down        # stop, keeping data
make nuke        # stop and delete the volumes
```

`make help` lists everything.

Once it is up:

| | |
|---|---|
| Wallet REST | http://localhost:8080 |
| Wallet gRPC | `localhost:9090` |
| Payment REST | http://localhost:8081 |
| Payment gRPC | `localhost:9091` |
| Grafana | http://localhost:3001 |
| Prometheus | http://localhost:9095 |
| Kafka UI | http://localhost:8085 |

To try the API, import
[`wallet-service/api/postman`](../wallet-service/api/postman) into Postman, select the
**Arcadia Local** environment, and run **Setup → Mint tokens**.

## Repository layout on disk

The services resolve the shared platform module through a `replace` directive pointing at
a sibling checkout, so the three repositories have to sit next to each other:

```
Arcadia/
├── infra/            ← you are here
├── wallet-service/
└── payment-service/
```

That `replace` is temporary scaffolding. Once `platform/` is published under a tag, the
directive goes away and each service depends on a version like any other module. Until
then, both service pipelines check out this repository alongside their own — see the
`Check out the shared platform module` step in either `ci.yml`.

---

## Why one PostgreSQL instance

The architecture document specifies Database-per-Service. This repository provisions
**one PostgreSQL container with one database and one role per service**, which is the same
pattern at a different scale.

What Database-per-Service actually requires is that no service can read or write another
service's data. That is enforced in
[`deploy/postgres/init/01-databases.sql`](deploy/postgres/init/01-databases.sql) by
grants, not by convention:

* each service owns a separate database with its own login role,
* `CONNECT` is revoked from `PUBLIC`, so a role can only reach its own database,
* the `public` schema is locked down inside each database.

`wallet_user` physically cannot see `payment_intents`, exactly as it could not if the two
lived on different hosts. No application query joins across databases, and no service
holds a DSN for another service's database.

**What is gained**: a laptop, a CI runner and a course demo need one container instead of
fourteen.

**What is given up**: independent failure and independent scaling of the storage layer. A
`shared_buffers` change affects everyone, and a runaway query on one database competes
for the same I/O as the others.

**How that is repaid**: the Kubernetes manifests point each service at its own hostname
(`postgres-wallet`, `postgres-payment`), so promoting a database to a dedicated
StatefulSet is a change to one connection string — no application code, no schema
migration, no join to untangle. The compose file is a development convenience that does
not constrain production.

---

## `contracts/` — the API definitions

Protobuf, compiled with [buf](https://buf.build).

```bash
make proto-lint       # style and consistency
make proto-gen        # regenerate platform/gen
make proto-breaking   # compare against main
```

Generated Go lands in `platform/gen/` and is **committed**. That is deliberate: a
service build should not require `protoc`, `buf` and two plugins to be installed at the
right versions, and a reviewer should be able to see in a diff when a wire contract
changed.

`proto-breaking` is the guard that matters. Renaming a field or renumbering one is a
breaking change for every deployed consumer, and it should fail in CI rather than in
production.

## `platform/` — the shared Go module

`github.com/MS-Arcadia/arcadia-platform`. The packages worth knowing about:

| Package | What it is for |
|---|---|
| `money` | Exact monetary arithmetic. Integer minor units, never floats. Basis points for rates, explicit rounding, and a `Allocate` that splits 70/30 without losing a unit. |
| `outbox` | The Transactional Outbox: a store that writes events in the caller's transaction, and a dispatcher that drains them with `FOR UPDATE SKIP LOCKED` so replicas share the work. |
| `inbox` | Consumer deduplication. Turns Kafka's at-least-once delivery into exactly-once processing. |
| `errs` | The error taxonomy. Domain code returns a semantic code; adapters translate it into a gRPC status or an RFC 7807 problem document. Internal details never cross the wire. |
| `authn` | JWT verification and RBAC. Pins the signing algorithm, so `alg: none` and HS/RS confusion attacks fail. |
| `kafkax` | Producer with `acks=all`, consumer groups with bounded retries and dead-letter routing. |
| `postgres` | Connection pool and the transaction manager the outbox pattern depends on. |
| `httpx` / `grpcx` | The two transports, with matching middleware and interceptor chains so a use case behaves identically whichever one invoked it. |
| `logx` | Structured JSON logging with trace correlation, and redaction by key name so a gift-card code cannot be logged by accident. |
| `otelx` / `metrics` / `health` | OpenTelemetry export, the Prometheus metric set, and liveness/readiness that are deliberately different things. |
| `migrate` | Embedded SQL migrations with checksums and an advisory lock, so concurrent pod starts cannot race. |
| `runtimex` | Process lifecycle: starting servers, consumers and schedulers together, and draining them in the right order. |

```bash
make platform-test
```

## `deploy/observability/`

Every service exports over OTLP to a single collector, which fans out to Prometheus,
Loki and Tempo. Nothing talks to a storage backend directly, so a backend can be replaced
without redeploying a service.

The parts worth reading:

* [`prometheus/alerts.yml`](deploy/observability/prometheus/alerts.yml) — the SLO table
  from the architecture document as executable rules. The severity split is the point:
  `page` wakes somebody up, `ticket` waits for business hours. Only things that can cost a
  user money, or that mean the ledger no longer adds up, page.
* [`grafana/provisioning/datasources/datasources.yml`](deploy/observability/grafana/provisioning/datasources/datasources.yml)
  — the correlations that make a slow metric lead to a trace, and a trace to the log lines
  it produced.
* [`grafana/provisioning/dashboards/wallet-payments.json`](deploy/observability/grafana/provisioning/dashboards/wallet-payments.json)
  — the top row is the health of the money path.

## `deploy/k8s/`

Kustomize, with a base and two overlays.

```bash
make k8s-build              # render all three targets
kubectl apply -k deploy/k8s/namespaces
kubectl apply -k deploy/k8s/overlays/staging
```

Namespaces are their own kustomization rather than part of the base, because an overlay
setting `namespace: arcadia-app` would rewrite the `Namespace` objects themselves and
collide.

Three things in the base are worth a look:

* **Probes.** `/livez` deliberately checks nothing while `/readyz` checks dependencies.
  Conflating them is how a database blip becomes a restart loop that makes an outage
  worse.
* **`NetworkPolicy`.** Default-deny with an explicit allow list, so a compromised pod in
  another service cannot reach the wallet's database. Note that the payment adapter's
  egress rule — the platform's only route to the public internet — excludes private ranges
  and the cloud metadata endpoint, so an SSRF through the gateway client cannot pivot back
  inside.
* **`Secret` placeholders.** Committed with obviously fake values so the manifests apply
  on a fresh cluster. Real values come from Vault or sealed-secrets. A plausible-looking
  placeholder is how a real secret ends up in git by accident.

---

## Event topics

| Topic | Producer | Consumed by |
|---|---|---|
| `wallet-events` | wallet | Store (saga replies), Notification, Auth (abuse flags) |
| `audit-events` | wallet | the immutable audit sink |
| `payment-events` | payment | wallet |
| `wallet-commands` | Store | wallet |
| `trade-events` | Marketplace | wallet |
| `user-events` | Auth | wallet, Profile, Notification |

Every consumed topic has a `<topic>.dlq` companion. Broker-side auto-creation is **off**:
each service declares the topics it owns at boot, so a typo fails loudly instead of
silently creating a topic nobody reads.

Messages share one envelope — `event_id`, `event_type`, `schema_version`, `occurred_at`,
`correlation_id`, `trace_id`, `payload` — and are partitioned by aggregate id, which is
what keeps two events about one wallet in order.

## Adding a service

1. Add its database and role to `deploy/postgres/init/01-databases.sql`. (Recreate the
   volume with `make nuke` — the init script only runs on an empty data directory.)
2. Add its `.proto` under `contracts/proto/arcadia/<service>/v1/` and run `make proto-gen`.
3. Add it to `deploy/compose/docker-compose.yml`.
4. Copy `deploy/k8s/base/wallet-service/` as a starting point.

The wallet service is the reference implementation. It is the most complete of the two,
and the one to imitate.
