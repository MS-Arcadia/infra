#!/usr/bin/env bash
# One-time migration: Deployment + standalone PVC  ->  StatefulSet + volumeClaimTemplate.
#
#   ./migrate-to-statefulset.sh postgres 01-postgres.yaml postgres-data data-postgres-0
#
# A StatefulSet's volumeClaimTemplate generates a PVC named
# <template>-<statefulset>-<ordinal>, which is not what the old Deployment's PVC
# is called — so pointing a StatefulSet at existing data means getting the data
# into a claim with the new name first. A StatefulSet adopts a PVC that already
# exists under the expected name rather than creating one, and that is the seam
# this script uses.
#
# The order matters and is the whole point:
#
#   1. scale the Deployment to zero      writes stop before anything is copied
#   2. create the destination PVC        empty, provisioned on the same node
#   3. copy old -> new inside the cluster  cp -a, both volumes mounted in one pod
#   4. apply the StatefulSet             adopts the PVC it finds
#   5. verify, then delete the Deployment
#
# The source PVC is deliberately NOT deleted. It stays as a point-in-time copy
# until someone confirms the new instance is healthy and removes it by hand.
set -euo pipefail

SERVICE=${1:?service name, e.g. postgres}
MANIFEST=${2:?manifest file, e.g. 01-postgres.yaml}
OLD_PVC=${3:?existing PVC, e.g. postgres-data}
NEW_PVC=${4:?PVC the StatefulSet expects, e.g. data-postgres-0}
NS=${NS:-arcadia}

# Alpine rather than a purpose-built image: this needs cp and nothing else, and
# an image already cached on the node starts in seconds.
COPY_IMAGE=${COPY_IMAGE:-docker.arvancloud.ir/alpine:3.20}

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "$SERVICE: checking preconditions"
kubectl -n "$NS" get pvc "$OLD_PVC" >/dev/null
SIZE=$(kubectl -n "$NS" get pvc "$OLD_PVC" -o jsonpath='{.spec.resources.requests.storage}')
echo "source PVC $OLD_PVC exists, $SIZE"

if kubectl -n "$NS" get statefulset "$SERVICE" >/dev/null 2>&1; then
  echo "StatefulSet/$SERVICE already exists — nothing to migrate."
  exit 0
fi

say "$SERVICE: stopping the Deployment so nothing writes during the copy"
if kubectl -n "$NS" get deployment "$SERVICE" >/dev/null 2>&1; then
  kubectl -n "$NS" scale deployment "$SERVICE" --replicas=0
  kubectl -n "$NS" wait --for=delete pod -l "app=$SERVICE" --timeout=120s 2>/dev/null || true
else
  echo "no Deployment/$SERVICE — assuming it was already removed"
fi

say "$SERVICE: creating $NEW_PVC ($SIZE)"
kubectl -n "$NS" get pvc "$NEW_PVC" >/dev/null 2>&1 || kubectl -n "$NS" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $NEW_PVC
  namespace: $NS
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: $SIZE
EOF

say "$SERVICE: copying $OLD_PVC -> $NEW_PVC"
kubectl -n "$NS" delete job "migrate-$SERVICE" --ignore-not-found >/dev/null
kubectl -n "$NS" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate-$SERVICE
  namespace: $NS
spec:
  backoffLimit: 1
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: copy
          image: $COPY_IMAGE
          # -a preserves ownership and mode, which postgres checks on startup and
          # refuses to run without. /old/. rather than /old so the contents land
          # in /new rather than in /new/old.
          command: [sh, -ceu]
          args:
            - |
              echo "source:"; ls -la /old
              cp -a /old/. /new/
              echo "destination:"; ls -la /new
              echo "bytes: \$(du -sb /new | cut -f1) (source \$(du -sb /old | cut -f1))"
          volumeMounts:
            - {name: old, mountPath: /old}
            - {name: new, mountPath: /new}
      volumes:
        - name: old
          persistentVolumeClaim: {claimName: $OLD_PVC}
        - name: new
          persistentVolumeClaim: {claimName: $NEW_PVC}
EOF
kubectl -n "$NS" wait --for=condition=complete "job/migrate-$SERVICE" --timeout=300s
kubectl -n "$NS" logs "job/migrate-$SERVICE" | tail -5

say "$SERVICE: applying the StatefulSet"
kubectl -n "$NS" apply -f "$MANIFEST"
kubectl -n "$NS" rollout status "statefulset/$SERVICE" --timeout=300s

say "$SERVICE: removing the old Deployment"
kubectl -n "$NS" delete deployment "$SERVICE" --ignore-not-found
kubectl -n "$NS" delete job "migrate-$SERVICE" --ignore-not-found

say "$SERVICE: done. $OLD_PVC was left in place as a backup:"
echo "    kubectl -n $NS delete pvc $OLD_PVC   # once you are satisfied"
