#!/usr/bin/env bash
# Apply the whole platform to a Kubernetes namespace.
#
#   ./apply.sh                 # apply everything
#   ./apply.sh --dry-run       # server-side validate, change nothing
#   ./apply.sh --config-only   # just the ConfigMaps and the Secret
#   NS=arcadia-staging ./apply.sh
#
# Why this exists. The manifests reference eight ConfigMaps and one Secret that
# nothing in them creates: their content lives in files elsewhere in this
# repository — the dashboards, the Prometheus scrape config, the Postgres init
# SQL — and `kubectl apply -f` has no way to build a ConfigMap from a directory.
# Those commands used to live only in comments, which meant the repository
# described a deployment nobody could reproduce without reading them.
#
# Everything here is idempotent: ConfigMaps and the Secret go through
# `--dry-run=client -o yaml | kubectl apply -f -` so a re-run updates in place
# rather than failing on "already exists".
#
# What this does NOT do, on purpose:
#   - create the namespace's TLS Secrets (cert-manager issues those)
#   - restart Deployments after a ConfigMap changes. A pod reads its ConfigMap
#     at start, so changing prometheus.yml here does not reach the running
#     Prometheus. `--restart` does that explicitly, because doing it implicitly
#     would roll the whole platform on an unrelated edit.
set -euo pipefail

cd "$(dirname "$0")"

NS=${NS:-arcadia}
KUBECTL=(kubectl -n "$NS")

DRY_RUN=""
CONFIG_ONLY=0
RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY_RUN="--dry-run=server" ;;
    --config-only) CONFIG_ONLY=1 ;;
    --restart)     RESTART=1 ;;
    -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# Pipe a generated object through apply, so every path is declarative and
# re-runnable. $DRY_RUN is unquoted on purpose: empty means "no flag".
apply() { kubectl apply ${DRY_RUN} -f - ; }

configmap() {
  local name=$1; shift
  "${KUBECTL[@]}" create configmap "$name" "$@" --dry-run=client -o yaml | apply
}

# --- namespace ---------------------------------------------------------------

say "namespace $NS"
kubectl create namespace "$NS" --dry-run=client -o yaml | apply

# --- secret ------------------------------------------------------------------
#
# deploy/k8s/.env is gitignored and holds the real values; .env.example documents
# the keys. Refusing to fall back to the example is deliberate — a platform that
# silently comes up with `change-me` as its JWT secret is worse than one that
# does not come up.

say "secret arcadia-secrets"
if [ ! -f .env ]; then
  echo "deploy/k8s/.env is missing." >&2
  echo "  cp .env.example .env   and fill in real values before deploying." >&2
  exit 1
fi
if grep -qE '=(change-me)?$' .env; then
  echo ".env still contains placeholder or empty values:" >&2
  grep -nE '=(change-me)?$' .env >&2
  exit 1
fi
"${KUBECTL[@]}" create secret generic arcadia-secrets --from-env-file=.env \
  --dry-run=client -o yaml | apply

# --- configmaps --------------------------------------------------------------
#
# Each one names the file it is built from. These are the eight the manifests
# mount; adding a ninth means adding it here, not in a comment.

say "configmaps"

# Postgres creates one database and role per service on first boot. Kept in
# deploy/postgres/init so the compose stack and the cluster share one source.
configmap postgres-init --from-file=../postgres/init/01-databases.sql

# Creates the media bucket and its scoped service account. Shared with compose.
configmap minio-setup-script --from-file=minio-setup.sh=../compose/minio-setup.sh

# Scrape config is cluster-specific (endpoint discovery); alert rules are shared
# with the compose stack.
configmap prometheus-config --from-file=prometheus.yml=observability/prometheus.yml
configmap prometheus-alerts --from-file=alerts.yml=../observability/alerts.yml

configmap alertmanager-config --from-file=alertmanager.yml=observability/alertmanager.yml

# Grafana: the datasources and dashboards are shared with compose; only the
# provider config differs, because the k8s deployment mounts the provider and
# the dashboard JSON from two separate ConfigMaps.
configmap grafana-datasources --from-file=../observability/grafana/datasources/
# Keyed as dashboards.yml, not after the source filename: the key becomes the
# filename inside /etc/grafana/provisioning/dashboards, and this is the name the
# running deployment already has.
configmap grafana-dashboards-provider --from-file=dashboards.yml=observability/dashboards-provider.yml
configmap grafana-dashboards-json --from-file=../observability/grafana/dashboards/

if [ "$CONFIG_ONLY" -eq 1 ]; then
  say "config only — stopping before the workloads"
  exit 0
fi

# --- workloads ---------------------------------------------------------------
#
# Applied in filename order, which is the dependency order: stateful
# infrastructure first, then the services that connect to it, then the edge,
# then observability and autoscaling.

say "workloads"
for manifest in [0-9]*.yaml; do
  printf '  %s\n' "$manifest"
  # minio-setup is a Job that has already completed, and a Job's pod template is
  # immutable — re-applying an unchanged one is an error rather than a no-op.
  # Tolerated here rather than deleted, because deleting it would re-run bucket
  # setup on every apply.
  if ! kubectl apply ${DRY_RUN} -f "$manifest" >/dev/null 2>&1; then
    output=$(kubectl apply ${DRY_RUN} -f "$manifest" 2>&1 || true)
    if grep -q 'field is immutable' <<<"$output"; then
      echo "      (skipped an immutable object that already exists)"
    else
      echo "$output" >&2
      exit 1
    fi
  fi
done

# --- optional restart --------------------------------------------------------

if [ "$RESTART" -eq 1 ] && [ -z "$DRY_RUN" ]; then
  say "restarting the deployments that read a ConfigMap"
  "${KUBECTL[@]}" rollout restart deployment/prometheus deployment/grafana deployment/alertmanager
fi

say "done"
[ -n "$DRY_RUN" ] && echo "(dry run — nothing was changed)" || {
  echo "  kubectl -n $NS get pods"
  echo "  ./apply.sh --restart   # if a ConfigMap changed and pods need to reload it"
}
