#!/usr/bin/env python3
"""Generate the Grafana dashboards from the service inventory.

    python3 generate-dashboards.py            # writes grafana/dashboards/
    python3 generate-dashboards.py --check    # fails if the tree is stale

Fifteen near-identical dashboards is not something to maintain by hand: the
panels differ only in which service they filter on and which metric family that
service speaks. Encoding that here means adding a service is one row in
SERVICES, and a panel improvement lands on all of them at once.

Two metric families exist, because the platform is written in two languages and
each stack instruments itself idiomatically:

    Python (FastAPI)  arcadia_http_requests_total{service,method,route,status}
                      arcadia_http_request_duration_seconds_bucket{...,le}
    Go                arcadia_rpc_requests_total{service,transport,method,code}
                      arcadia_rpc_duration_seconds_bucket{...,le}

They are not unified into one name on purpose — that would mean a coordinated
rename across thirteen repositories to gain nothing a `or` in PromQL does not
already give. What matters is that both carry a `service` label, so the overview
can union them and still group by service.

The gateway is a third case: it labels by public *prefix* rather than by route,
since a path label on a storefront produces one series per game id.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
OUT = HERE / "grafana" / "dashboards"

# Dashboards generated from this file. Anything else in OUT is hand-written and
# left alone — wallet-payments.json predates this generator and stays authored.
GENERATED_PREFIX = "gen-"

PY = "python"
GO = "go"

# name, port, stack. Order is the order they appear in Grafana's list.
SERVICES = [
    ("auth-profile-service", 8085, PY),
    ("catalog-service", 8082, PY),
    ("order-service", 8083, PY),
    ("wallet-service", 8080, GO),
    ("payment-service", 8081, GO),
    ("media-service", 8084, PY),
    ("notification-service", 8086, PY),
    ("marketplace-service", 8087, GO),
    ("review-service", 8088, PY),
    ("festival-service", 8089, PY),
    ("community-service", 8091, PY),
    ("search-service", 8092, PY),
    ("recommendation-service", 8093, PY),
]

DS = {"type": "prometheus", "uid": "prometheus"}


def requests_total(stack: str) -> str:
    return "arcadia_http_requests_total" if stack == PY else "arcadia_rpc_requests_total"


def duration_bucket(stack: str) -> str:
    return (
        "arcadia_http_request_duration_seconds_bucket"
        if stack == PY
        else "arcadia_rpc_duration_seconds_bucket"
    )


def status_label(stack: str) -> str:
    """Python names it `status`, Go names it `code`. Both hold an HTTP status."""
    return "status" if stack == PY else "code"


def breakdown_label(stack: str) -> str:
    """What a request is grouped by within one service."""
    return "route" if stack == PY else "method"


def panel(kind, title, x, y, w, h, targets, *, unit=None, desc=None, extra=None):
    fc = {"defaults": {"custom": {"fillOpacity": 10}}}
    if unit:
        fc["defaults"]["unit"] = unit
    p = {
        "type": kind,
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [dict(refId=chr(65 + i), **t) for i, t in enumerate(targets)],
        "fieldConfig": fc,
    }
    if desc:
        p["description"] = desc
    if extra:
        p.update(extra)
    return p


def stat(title, x, y, w, h, expr, *, desc=None, unit=None, steps=None):
    p = panel(
        "stat", title, x, y, w, h, [{"expr": expr}], unit=unit, desc=desc,
        extra={"options": {"colorMode": "background", "reduceOptions": {"calcs": ["lastNotNull"]}}},
    )
    if steps:
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": steps}
    return p


GREEN_THEN_RED = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
RED_THEN_GREEN = [{"color": "red", "value": None}, {"color": "green", "value": 1}]


def service_dashboard(name: str, port: int, stack: str) -> dict:
    """RED for one service, plus what its pod is costing and its event pipeline."""
    reqs = requests_total(stack)
    bucket = duration_bucket(stack)
    status = status_label(stack)
    by = breakdown_label(stack)
    sel = f'service="{name}"'
    # cAdvisor labels by pod, and a pod name is the Deployment name plus two
    # random suffixes — hence a regex rather than an equality match.
    pod = f'namespace="arcadia", pod=~"{name}-.*", container!=""'

    panels = [
        stat("Up", 0, 0, 4, 4, f'up{{job="{name}"}}',
             desc="Whether Prometheus can reach this service's /metrics at all.",
             steps=RED_THEN_GREEN),
        stat("Request rate", 4, 0, 5, 4, f"sum(rate({reqs}{{{sel}}}[5m]))",
             unit="reqps", desc="RED: rate."),
        stat("Error rate", 9, 0, 5, 4,
             f'sum(rate({reqs}{{{sel}, {status}=~"5.."}}[5m])) / clamp_min(sum(rate({reqs}{{{sel}}}[5m])), 1e-9)',
             unit="percentunit", desc="RED: errors. 5xx only — a 4xx is the caller's mistake, not this service's.",
             steps=[{"color": "green", "value": None}, {"color": "yellow", "value": 0.01}, {"color": "red", "value": 0.05}]),
        stat("Latency p95", 14, 0, 5, 4,
             f"histogram_quantile(0.95, sum by (le) (rate({bucket}{{{sel}}}[5m])))",
             unit="s", desc="RED: duration. The platform's objective is 300ms.",
             steps=[{"color": "green", "value": None}, {"color": "red", "value": 0.3}]),
        stat("Restarts (1h)", 19, 0, 5, 4,
             f'sum(increase(kube_pod_container_status_restarts_total{{namespace="arcadia", pod=~"{name}-.*"}}[1h])) or vector(0)',
             desc="Crash-loops. Needs kube-state-metrics; reads 0 without it.",
             steps=GREEN_THEN_RED),

        panel("timeseries", f"Request rate by {by}", 0, 4, 12, 8,
              [{"expr": f"sum by ({by}) (rate({reqs}{{{sel}}}[5m]))", "legendFormat": "{{" + by + "}}"}],
              unit="reqps"),
        panel("timeseries", "Responses by status", 12, 4, 12, 8,
              [{"expr": f"sum by ({status}) (rate({reqs}{{{sel}}}[5m]))", "legendFormat": "{{" + status + "}}"}],
              unit="reqps", extra={"fieldConfig": {"defaults": {"unit": "reqps", "custom": {"fillOpacity": 10, "stacking": {"mode": "normal"}}}}}),

        panel("timeseries", "Latency percentiles", 0, 12, 12, 8,
              [{"expr": f"histogram_quantile(0.50, sum by (le) (rate({bucket}{{{sel}}}[5m])))", "legendFormat": "p50"},
               {"expr": f"histogram_quantile(0.95, sum by (le) (rate({bucket}{{{sel}}}[5m])))", "legendFormat": "p95"},
               {"expr": f"histogram_quantile(0.99, sum by (le) (rate({bucket}{{{sel}}}[5m])))", "legendFormat": "p99"}],
              unit="s"),
        panel("timeseries", f"Slowest {by}s (p95)", 12, 12, 12, 8,
              [{"expr": f"topk(5, histogram_quantile(0.95, sum by ({by}, le) (rate({bucket}{{{sel}}}[5m]))))",
                "legendFormat": "{{" + by + "}}"}],
              unit="s", desc="Top 5 only: on a service with many routes the full set is unreadable."),

        panel("timeseries", "CPU", 0, 20, 8, 7,
              [{"expr": f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{pod}}}[5m]))", "legendFormat": "{{pod}}"}],
              unit="short", desc="Cores. Compare against the limit in infra/deploy/k8s — a pod at its ceiling is throttled, not crashed."),
        panel("timeseries", "Memory", 8, 20, 8, 7,
              [{"expr": f"sum by (pod) (container_memory_working_set_bytes{{{pod}}})", "legendFormat": "{{pod}}"}],
              unit="bytes"),
        panel("timeseries", "Replicas", 16, 20, 8, 7,
              [{"expr": f'count(up{{job="{name}"}})', "legendFormat": "scraped targets"}],
              desc="Scrape targets answering, which is the closest thing to a replica count without kube-state-metrics."),

        panel("timeseries", "Outbox backlog", 0, 27, 8, 7,
              [{"expr": f"arcadia_outbox_backlog{{{sel}}}", "legendFormat": "{{status}}"}],
              desc="Events written but not yet published. Empty for services that own no topic."),
        panel("timeseries", "Oldest unpublished event", 8, 27, 8, 7,
              [{"expr": f"arcadia_outbox_oldest_pending_age_seconds{{{sel}}}", "legendFormat": "age"}],
              unit="s"),
        panel("timeseries", "Consumer lag", 16, 27, 8, 7,
              [{"expr": f"arcadia_kafka_consumer_lag{{{sel}}}", "legendFormat": "{{topic}}"}],
              desc="Empty for services that consume nothing."),
    ]

    return {
        "uid": f"arcadia-svc-{name}",
        "title": f"Arcadia — {name}",
        "description": f"RED metrics, resource cost and event-pipeline health for {name} (port {port}, {stack} stack).",
        "tags": ["arcadia", "service", stack],
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": panels,
    }


def gateway_dashboard() -> dict:
    """The edge labels by public prefix, not route — so it gets its own layout."""
    pod = 'namespace="arcadia", pod=~"api-gateway-.*", container!=""'
    panels = [
        stat("Up", 0, 0, 4, 4, 'up{job="api-gateway"}', steps=RED_THEN_GREEN),
        stat("Request rate", 4, 0, 5, 4, "sum(rate(gateway_requests_total[5m]))", unit="reqps"),
        stat("5xx rate", 9, 0, 5, 4,
             'sum(rate(gateway_requests_total{status="5xx"}[5m])) / clamp_min(sum(rate(gateway_requests_total[5m])), 1e-9)',
             unit="percentunit",
             steps=[{"color": "green", "value": None}, {"color": "yellow", "value": 0.01}, {"color": "red", "value": 0.05}]),
        stat("Latency p95", 14, 0, 5, 4,
             "histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket[5m])))",
             unit="s", steps=[{"color": "green", "value": None}, {"color": "red", "value": 0.3}]),
        stat("Replicas", 19, 0, 5, 4, 'count(up{job="api-gateway"})',
             desc="The gateway runs a minimum of three; anything less means pods are down."),

        panel("timeseries", "Traffic by service prefix", 0, 4, 12, 8,
              [{"expr": "sum by (prefix) (rate(gateway_requests_total[5m]))", "legendFormat": "{{prefix}}"}],
              unit="reqps",
              desc="Which service the platform is actually being asked for. Labelled by prefix rather than path so a catalogue of games cannot explode the cardinality.",
              extra={"fieldConfig": {"defaults": {"unit": "reqps", "custom": {"fillOpacity": 10, "stacking": {"mode": "normal"}}}}}),
        panel("timeseries", "Responses by status class", 12, 4, 12, 8,
              [{"expr": "sum by (status) (rate(gateway_requests_total[5m]))", "legendFormat": "{{status}}"}],
              unit="reqps",
              extra={"fieldConfig": {"defaults": {"unit": "reqps", "custom": {"fillOpacity": 10, "stacking": {"mode": "normal"}}}}}),

        panel("timeseries", "Latency by prefix (p95)", 0, 12, 12, 8,
              [{"expr": "histogram_quantile(0.95, sum by (prefix, le) (rate(gateway_request_duration_seconds_bucket[5m])))",
                "legendFormat": "{{prefix}}"}],
              unit="s"),
        panel("timeseries", "Upstream failures", 12, 12, 12, 8,
              [{"expr": "sum by (upstream) (rate(gateway_upstream_failures_total[5m]))", "legendFormat": "{{upstream}}"}],
              desc="Requests that could not reach their service or timed out. This is the panel that names which backend is down."),

        panel("timeseries", "Rejected at the edge", 0, 20, 12, 7,
              [{"expr": "rate(gateway_rate_limited_total[5m])", "legendFormat": "rate limited"},
               {"expr": "rate(gateway_tokens_rejected_total[5m])", "legendFormat": "bad tokens"}],
              desc="Refused before any service paid for them. A spike in bad tokens is worth looking at."),
        panel("timeseries", "CPU / memory per pod", 12, 20, 12, 7,
              [{"expr": f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{pod}}}[5m]))", "legendFormat": "cpu {{pod}}"}],
              unit="short"),
    ]
    return {
        "uid": "arcadia-svc-api-gateway",
        "title": "Arcadia — api-gateway",
        "description": "The platform's single public entry point. Labelled by public prefix, so this is also the fastest way to see which service traffic is going to.",
        "tags": ["arcadia", "service", "go"],
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": panels,
    }


def union(py_expr: str, go_expr: str) -> str:
    """Both metric families in one series set, for cross-service panels."""
    return f"({py_expr}) or ({go_expr})"


def overview_dashboard() -> dict:
    py_reqs, go_reqs = "arcadia_http_requests_total", "arcadia_rpc_requests_total"
    py_bucket, go_bucket = "arcadia_http_request_duration_seconds_bucket", "arcadia_rpc_duration_seconds_bucket"

    rate_all = union(f"sum by (service) (rate({py_reqs}[5m]))", f"sum by (service) (rate({go_reqs}[5m]))")
    err_all = union(
        f'sum by (service) (rate({py_reqs}{{status=~"5.."}}[5m])) / clamp_min(sum by (service) (rate({py_reqs}[5m])), 1e-9)',
        f'sum by (service) (rate({go_reqs}{{code=~"5.."}}[5m])) / clamp_min(sum by (service) (rate({go_reqs}[5m])), 1e-9)',
    )
    p95_all = union(
        f"histogram_quantile(0.95, sum by (service, le) (rate({py_bucket}[5m])))",
        f"histogram_quantile(0.95, sum by (service, le) (rate({go_bucket}[5m])))",
    )

    panels = [
        stat("Targets up", 0, 0, 6, 5, 'sum(up{job!="kubernetes-cadvisor"})',
             desc="Every service that answers /metrics. Compare with the total on the right.",
             steps=RED_THEN_GREEN),
        panel("table", "Service status", 6, 0, 18, 5,
              [{"expr": 'up{job!="kubernetes-cadvisor"}', "format": "table", "instant": True}],
              desc="One row per scrape target. 0 means Prometheus cannot reach that service at all.",
              extra={
                  "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "instance": True, "__name__": True}}}],
                  "fieldConfig": {
                      "defaults": {"custom": {"cellOptions": {"type": "color-background"}},
                                   "thresholds": {"mode": "absolute", "steps": RED_THEN_GREEN}},
                      "overrides": [{"matcher": {"id": "byName", "options": "Value"},
                                     "properties": [{"id": "mappings", "value": [{"type": "value", "options": {"0": {"text": "DOWN"}, "1": {"text": "UP"}}}]}]}],
                  },
              }),

        panel("timeseries", "Request rate by service", 0, 5, 12, 8,
              [{"expr": rate_all, "legendFormat": "{{service}}"}], unit="reqps",
              desc="Both metric families unioned — Python services report arcadia_http_*, Go services arcadia_rpc_*.",
              extra={"fieldConfig": {"defaults": {"unit": "reqps", "custom": {"fillOpacity": 10, "stacking": {"mode": "normal"}}}}}),
        panel("timeseries", "Error rate by service", 12, 5, 12, 8,
              [{"expr": err_all, "legendFormat": "{{service}}"}], unit="percentunit",
              desc="5xx as a share of each service's own traffic.",
              extra={"fieldConfig": {"defaults": {"unit": "percentunit", "custom": {"fillOpacity": 10},
                                                 "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 0.01}, {"color": "red", "value": 0.05}]}}}}),

        panel("timeseries", "Latency p95 by service", 0, 13, 12, 8,
              [{"expr": p95_all, "legendFormat": "{{service}}"}], unit="s",
              desc="The platform's objective is 300ms."),
        panel("timeseries", "Gateway traffic by prefix", 12, 13, 12, 8,
              [{"expr": "sum by (prefix) (rate(gateway_requests_total[5m]))", "legendFormat": "{{prefix}}"}],
              unit="reqps", desc="What the outside world is actually asking for.",
              extra={"fieldConfig": {"defaults": {"unit": "reqps", "custom": {"fillOpacity": 10, "stacking": {"mode": "normal"}}}}}),

        panel("timeseries", "Pod CPU", 0, 21, 12, 8,
              [{"expr": 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="arcadia", container!=""}[5m]))', "legendFormat": "{{pod}}"}],
              unit="short",
              extra={"fieldConfig": {"defaults": {"unit": "short", "custom": {"fillOpacity": 10, "stacking": {"mode": "normal"}}}}}),
        panel("timeseries", "Pod memory", 12, 21, 12, 8,
              [{"expr": 'sum by (pod) (container_memory_working_set_bytes{namespace="arcadia", container!=""})', "legendFormat": "{{pod}}"}],
              unit="bytes",
              extra={"fieldConfig": {"defaults": {"unit": "bytes", "custom": {"fillOpacity": 10, "stacking": {"mode": "normal"}}}}}),

        panel("timeseries", "Outbox backlog", 0, 29, 8, 7,
              [{"expr": "sum by (service, status) (arcadia_outbox_backlog)", "legendFormat": "{{service}} {{status}}"}],
              desc="Events written inside a transaction but not yet published. A rising line means consumers are falling behind."),
        panel("timeseries", "Consumer lag", 8, 29, 8, 7,
              [{"expr": "arcadia_kafka_consumer_lag", "legendFormat": "{{service}} {{topic}}"}]),
        panel("timeseries", "Dead-lettered events", 16, 29, 8, 7,
              [{"expr": "sum by (service, topic) (increase(arcadia_events_dead_lettered_total[10m]))", "legendFormat": "{{service}} {{topic}}"}],
              desc="Each one is a business operation that did not happen. Should stay at zero.",
              extra={"fieldConfig": {"defaults": {"custom": {"fillOpacity": 10}, "thresholds": {"mode": "absolute", "steps": GREEN_THEN_RED}}}}),
    ]

    return {
        "uid": "arcadia-platform-overview",
        "title": "Arcadia — Platform Overview",
        "description": "Every service in one place: what is up, how much traffic it takes, how much of that fails, how slow it is, and what each pod costs.",
        "tags": ["arcadia", "overview"],
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": panels,
    }


def build() -> dict[str, dict]:
    out = {f"{GENERATED_PREFIX}platform-overview.json": overview_dashboard(),
           f"{GENERATED_PREFIX}api-gateway.json": gateway_dashboard()}
    for name, port, stack in SERVICES:
        out[f"{GENERATED_PREFIX}{name}.json"] = service_dashboard(name, port, stack)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the committed dashboards are stale")
    args = parser.parse_args()

    dashboards = build()
    OUT.mkdir(parents=True, exist_ok=True)
    stale = []

    for filename, body in dashboards.items():
        rendered = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
        path = OUT / filename
        if args.check:
            if not path.exists() or path.read_text() != rendered:
                stale.append(filename)
        else:
            path.write_text(rendered)

    # A dashboard whose service was removed from SERVICES must not linger.
    orphans = [p.name for p in OUT.glob(f"{GENERATED_PREFIX}*.json") if p.name not in dashboards]
    if args.check:
        if stale or orphans:
            print("dashboards are out of date; run generate-dashboards.py", file=sys.stderr)
            for f in stale:
                print(f"  stale:  {f}", file=sys.stderr)
            for f in orphans:
                print(f"  orphan: {f}", file=sys.stderr)
            return 1
        print(f"{len(dashboards)} dashboards up to date")
        return 0

    for name in orphans:
        (OUT / name).unlink()
        print(f"removed {name}")
    print(f"wrote {len(dashboards)} dashboards to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
