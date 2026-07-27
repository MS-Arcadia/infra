# Arcadia — Infrastructure

Deployment files for the [Arcadia](../PHASE01/README.md) platform. **This repository
contains no source code and compiles nothing.** It runs images that the service
repositories build.

```
infra/
├── deploy/
│   ├── compose/        the local platform
│   ├── postgres/init/  one database and role per service
│   └── observability/  Prometheus + Grafana (opt-in)
└── docs/runbooks/
```

## Getting started

Build the images first, in the service repositories, then start the platform:

```bash
make images      # calls each service's own `make docker`
make up
make wait        # blocks until both services report ready
```

Or if the images already exist, just `make up`. `make help` lists the rest.

Docker is the whole deployment story here — there is no Kubernetes, no Helm and no
Terraform. Both service images carry a `HEALTHCHECK`, so `make ps` tells you whether the
platform is actually up rather than merely started.

| Service | | Language |
|---|---|---|
| Wallet | http://localhost:8080 · gRPC `:9090` | Go |
| Payment | http://localhost:8081 · gRPC `:9091` | Go |
| Catalog | http://localhost:8082 · [docs](http://localhost:8082/docs) | Python |
| Order | http://localhost:8083 · [docs](http://localhost:8083/docs) | Python |
| Media | http://localhost:8084 · [docs](http://localhost:8084/docs) | Python |

```bash
curl -s localhost:8080/readyz
```

To exercise the API, import [`wallet-service/api/postman`](../wallet-service/api/postman)
and run **Setup → Mint tokens**.

## What runs, and what it costs

Eight containers by default, roughly 1.8 GB with the limits set in the compose file:

| | Memory limit | |
|---|---|---|
| `postgres` | 384M | one database and role per service, five of them |
| `kafka` | 640M | KRaft mode, no ZooKeeper, one partition per topic |
| `redis` | 96M | gift-card rate-limit windows only, no persistence |
| `wallet-service` | 128M | Go |
| `payment-service` | 128M | Go |
| `catalog-service` | 160M | Python; one uvicorn worker |
| `order-service` | 160M | Python; one uvicorn worker |
| `media-service` | 160M | Python; uploads stream, so this does not scale with file size |

The Python services get 160M rather than 128M because a CPython process with SQLAlchemy and
aiokafka loaded starts higher than a Go binary does. One uvicorn worker each: a second worker
inside the container would double the database pool and the Kafka consumers without doubling
anything the service is short of.

The media service is the one worth watching. Its limit does **not** scale with upload size —
uploads and downloads both stream in 1 MiB chunks, so a 4 GB game build passes through a
160 MB container. What does grow is its **disk**, which is why it has a volume and an alert.

Metrics are opt-in and add two more:

```bash
make up-metrics      # + Prometheus (256M) and Grafana (256M)
```

There is no OpenTelemetry collector, no Loki and no Tempo. Prometheus scrapes each
service's `/metrics` directly, and log correlation works through the correlation id the
services already stamp on every line — which answers "show me everything for this
purchase" without three more containers to run. The collector earns its place when
several backends need the same signals; with one backend and two services it is a
container that only forwards.

---

## Why one PostgreSQL instance

The architecture document specifies Database-per-Service. This runs **one PostgreSQL
container with one database and one role per service**, which is the same pattern at a
different scale.

What Database-per-Service actually requires is that no service can read or write another
service's data. That is enforced in
[`deploy/postgres/init/01-databases.sql`](deploy/postgres/init/01-databases.sql) by
grants, not by convention: `CONNECT` is revoked from `PUBLIC`, so `wallet_user`
physically cannot reach `payment_intents` — exactly as it could not if the two lived on
different hosts.

**What is given up** is independent failure and scaling of the storage layer. **How that
is repaid**: a service knows nothing about storage beyond its own `DATABASE_URL`, so
promoting one database to its own instance — a second Postgres container here, a managed
instance later — is a change to one connection string. No application query joins across
databases, so nothing prevents it.

## Event topics

| Topic | Producer | Consumed by |
|---|---|---|
| `wallet-events` | wallet | **order (saga replies)**, Notification, Auth (abuse flags) |
| `audit-events` | wallet | the audit sink |
| `payment-events` | payment | wallet |
| `wallet-commands` | **order** | wallet |
| `game-events` | **catalog** | **order (ownership replies)**, Search, Festival, Profile |
| `catalog-commands` | **order** | **catalog** |
| `purchase-events` | **order** | Notification, Recommendation, Profile |
| `media-events` | **media** | Search, Profile |
| `trade-events` | Marketplace | wallet |
| `user-events` | Auth | wallet, Profile, Notification |

The two **`-commands`** topics are different from the rest. They are addressed to exactly one
service, so an unrecognised message on them is dead-lettered rather than ignored: it means the
sender is issuing a command the receiver does not implement, which is a contract violation
worth an operator's attention.

Everything ending in `-events` is shared. A consumer there ignores what it does not handle —
`wallet-events` carries every balance change on the platform, and the order service has no
business dead-lettering a gift-card redemption.

Every consumed topic has a `<topic>.dlq` companion. Broker-side auto-creation is **off**:
each service declares the topics it owns at boot, so a typo fails loudly rather than
silently creating a topic nobody reads.

## What is deliberately not here

**No Kubernetes, no Helm, no Terraform.** The platform runs on plain Docker, so the only
deployment description is the compose file. Manifests for a cluster nobody has go stale
before they are ever applied; the compose file is exercised every time somebody runs
`make up`, which is the only reason to trust a deployment description at all.

The things a cluster would have given us and how they are covered meanwhile:

* **Health.** Both images declare a `HEALTHCHECK` that calls the binary's own
  `healthcheck` subcommand — the images are distroless, so there is no `curl` or shell
  for the usual form to invoke. It probes `/readyz`, not `/livez`: `/livez` deliberately
  checks nothing, and conflating the two is how a database blip becomes a restart loop
  that makes an outage worse.
* **Restarts.** `restart: unless-stopped` on every container.
* **Memory limits, but no CPU limits.** Throttling a service that holds database row
  locks turns a latency spike into lock contention across the platform; a memory leak
  should be killed rather than starve its neighbours.
* **Network isolation.** Compose puts everything on one bridge network and only the
  ports in `.env` reach the host. The finer-grained thing worth wanting is an egress
  restriction on the payment adapter, the platform's only route to the public internet —
  that needs a real network policy, and it is the first thing to add if this ever moves
  to a cluster.

## Alerts

[`deploy/observability/alerts.yml`](deploy/observability/alerts.yml) is the SLO table from
the architecture document as executable rules. The severity split is the point: `page`
wakes somebody up, `ticket` waits for business hours. Only things that mean money was
lost, or that the ledger no longer adds up, page — an alert set where everything is urgent
trains people to ignore all of it.

`LedgerMismatch` has a runbook: [`docs/runbooks/ledger-mismatch.md`](docs/runbooks/ledger-mismatch.md).

## Adding a service

1. Add its database and role to `deploy/postgres/init/01-databases.sql`, then `make nuke`
   — the init script only runs on an empty data directory.
2. Add it to `deploy/compose/docker-compose.yml` with an `image:` line. It builds its own
   image; this repository does not.
3. Add a scrape target to `deploy/observability/prometheus.yml`.
4. Add its `make docker` to the `images` target and bump `SERVICE_COUNT` in the Makefile, so
   `make wait` knows how many containers to expect.
