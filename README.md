# Arcadia — Infrastructure

Deployment files for the [Arcadia](../PHASE01/README.md) platform. **This repository
contains no source code and compiles nothing.** It runs images that the service
repositories build.

```
infra/
├── deploy/
│   ├── compose/        the local platform
│   ├── postgres/init/  one database and role per service
│   ├── observability/  Prometheus + Grafana (opt-in)
│   └── k8s/            one manifest per service
└── docs/runbooks/
```

## Getting started

Build the images first, in the service repositories, then start the platform:

```bash
make images      # calls each service's own `make docker`
make up
```

Or if the images already exist, just `make up`. `make help` lists the rest.

| | |
|---|---|
| Wallet REST | http://localhost:8080 |
| Wallet gRPC | `localhost:9090` |
| Payment REST | http://localhost:8081 |
| Payment gRPC | `localhost:9091` |

```bash
curl -s localhost:8080/readyz
```

To exercise the API, import [`wallet-service/api/postman`](../wallet-service/api/postman)
and run **Setup → Mint tokens**.

## What runs, and what it costs

Five containers by default, roughly 1.2 GB with the limits set in the compose file:

| | Memory limit | |
|---|---|---|
| `postgres` | 384M | one database and role per service |
| `kafka` | 640M | KRaft mode, no ZooKeeper, one partition per topic |
| `redis` | 96M | gift-card rate-limit windows only, no persistence |
| `wallet-service` | 128M | |
| `payment-service` | 128M | |

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
is repaid**: the Kubernetes manifests point each service at its own hostname
(`postgres-wallet`, `postgres-payment`), so promoting a database to a dedicated instance
is a change to one connection string. No application query joins across databases, so
nothing prevents it.

## Event topics

| Topic | Producer | Consumed by |
|---|---|---|
| `wallet-events` | wallet | Store (saga replies), Notification, Auth (abuse flags) |
| `audit-events` | wallet | the audit sink |
| `payment-events` | payment | wallet |
| `wallet-commands` | Store | wallet |
| `trade-events` | Marketplace | wallet |
| `user-events` | Auth | wallet, Profile, Notification |

Every consumed topic has a `<topic>.dlq` companion. Broker-side auto-creation is **off**:
each service declares the topics it owns at boot, so a typo fails loudly rather than
silently creating a topic nobody reads.

## Kubernetes

One file per service plus namespaces — no kustomize base and overlays. With two services
and one environment, overlays add indirection without answering a question anybody is
asking; `kubectl kustomize` can be layered on top of these files unchanged when a second
environment appears.

```bash
kubectl apply -f deploy/k8s/namespaces.yaml
kubectl apply -f deploy/k8s/wallet-service.yaml
kubectl apply -f deploy/k8s/payment-service.yaml
```

Replace every `REPLACE_ME` in the `Secret` blocks first. They are committed with obvious
placeholders so the manifests apply on a fresh cluster; a plausible-looking default is
how a real credential ends up in git. CI checks that they are still placeholders.

Three things in there are worth reading:

* **Probes.** `/livez` deliberately checks nothing while `/readyz` checks dependencies.
  Conflating them is how a database blip becomes a restart loop that makes an outage
  worse.
* **`NetworkPolicy`.** Default-deny with an explicit allow list. The payment adapter's
  egress rule — the platform's only route to the public internet — excludes private ranges
  and the cloud metadata endpoint, so an SSRF through the gateway client cannot pivot back
  inside.
* **No CPU limit, but a memory limit.** Throttling a service that holds database row locks
  turns a latency spike into lock contention across the platform; a memory leak should be
  killed rather than evict its neighbours.

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
3. Copy `deploy/k8s/wallet-service.yaml` as a starting point.
4. Add a scrape target to `deploy/observability/prometheus.yml`.
