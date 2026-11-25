#!/usr/bin/env bash
# End-to-end test of the Kubernetes manifests on a throwaway kind cluster:
# secrets (as docs/rollout.md creates them) -> database step -> gateway -> smoke test -> backup.
# Usage: ci/kind-e2e.sh   (IMAGE=... to test another tag; KEEP_CLUSTER=1 to keep it)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cluster=airgaped-litellm-e2e
ns=litellm-e2e
image="${IMAGE:-airgaped-litellm:ci}"
k=(kubectl --context "kind-$cluster" -n "$ns")
pf_pid=""
# shellcheck disable=SC1091
source "$root/versions.env"

cleanup() {
  local code=$?
  [ -n "$pf_pid" ] && kill "$pf_pid" 2>/dev/null || true
  if [ "$code" -ne 0 ]; then
    echo "--- e2e failed; diagnostics"
    "${k[@]}" get pods,jobs,pvc -o wide 2>/dev/null || true
    "${k[@]}" get events --sort-by=.lastTimestamp 2>/dev/null | tail -30 || true
    for pod in $("${k[@]}" get pods -o name 2>/dev/null); do
      echo "--- logs $pod"; "${k[@]}" logs "$pod" --all-containers --tail=40 2>/dev/null || true
    done
  fi
  if [ "${KEEP_CLUSTER:-0}" != 1 ]; then kind delete cluster --name "$cluster" >/dev/null 2>&1 || true; fi
  exit "$code"
}
trap cleanup EXIT

step() { echo "=== $*"; }

step "cluster"
kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
kind create cluster --name "$cluster" --wait 120s
docker pull -q "$POSTGRES_UPSTREAM" >/dev/null
docker tag "$POSTGRES_UPSTREAM" e2e/postgres:pinned
if [ "$image" != airgaped-litellm:ci ]; then docker tag "$image" airgaped-litellm:ci; fi
# Save single-platform archives: `kind load docker-image` fails on multi-platform
# images in Docker's containerd image store.
case "$(docker info --format '{{.Architecture}}')" in
  aarch64|arm64) platform=linux/arm64 ;; *) platform=linux/amd64 ;;
esac
archives="$(mktemp -d)"
for img in airgaped-litellm:ci e2e/postgres:pinned; do
  docker save --platform "$platform" -o "$archives/image.tar" "$img"
  kind load image-archive --name "$cluster" "$archives/image.tar"
done
rm -rf "$archives"

step "namespace and secrets (same commands as docs/rollout.md, fake values)"
kubectl --context "kind-$cluster" create namespace "$ns"
pg_password=e2e-postgres-password
"${k[@]}" create secret generic litellm-postgres --from-literal=POSTGRES_PASSWORD="$pg_password"
"${k[@]}" create secret generic litellm-secrets \
  --from-literal=LITELLM_MASTER_KEY=sk-e2e-master \
  --from-literal=LITELLM_SALT_KEY=sk-e2e-salt-0123456789 \
  --from-literal=DATABASE_URL="postgresql://litellm:$pg_password@litellm-postgres:5432/litellm" \
  --from-literal=CORP_LITELLM_URL=http://mock:8080 \
  --from-literal=CORP_LITELLM_KEY=sk-corp-test \
  --from-literal=AWS_BEARER_TOKEN_BEDROCK=ABSK-test-only \
  --from-literal=AWS_BEDROCK_RUNTIME_ENDPOINT=http://mock:8080
"${k[@]}" create configmap mock-upstream --from-file=mock_upstream.py="$root/ci/mock_upstream.py"

step "database step: Postgres + migrations"
kubectl --context "kind-$cluster" apply -k "$root/ci/kind/migrate"
"${k[@]}" wait --for=condition=complete job/litellm-migrate --timeout=600s

step "gateway"
kubectl --context "kind-$cluster" apply -k "$root/ci/kind"
"${k[@]}" rollout status deploy/litellm --timeout=600s
"${k[@]}" rollout status deploy/mock --timeout=120s

step "smoke test"
"${k[@]}" port-forward svc/litellm 14000:4000 >/dev/null 2>&1 &
pf_pid=$!
for _ in $(seq 30); do
  curl -fsS http://127.0.0.1:14000/health/liveliness >/dev/null 2>&1 && break; sleep 1
done
out="$(LITELLM_MASTER_KEY=sk-e2e-master python3 "$root/scripts/smoke_test.py" --base-url http://127.0.0.1:14000)"
echo "$out"
[ "$(grep -c '^PASS ' <<<"$out")" -eq 4 ] || { echo "FAIL expected 4 PASS lines"; exit 1; }

step "every key gets an expiry (litellm-key-expiry job)"
state="$(mktemp)"
LITELLM_MASTER_KEY=sk-e2e-master python3 "$root/ci/expiry_check.py" make --base-url http://127.0.0.1:14000 --state "$state"
"${k[@]}" create job --from=cronjob/litellm-key-expiry expiry-now
"${k[@]}" wait --for=condition=complete job/expiry-now --timeout=300s
"${k[@]}" logs job/expiry-now
LITELLM_MASTER_KEY=sk-e2e-master python3 "$root/ci/expiry_check.py" verify --base-url http://127.0.0.1:14000 --state "$state"
rm -f "$state"

step "backup and restore check"
"${k[@]}" create job --from=cronjob/litellm-backup backup-now
"${k[@]}" wait --for=condition=complete job/backup-now --timeout=300s
# Run the check as a Job and read its logs (kubectl run -i can miss short-lived output).
"${k[@]}" apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: backup-check
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 70
      containers:
        - name: backup-check
          image: e2e/postgres:pinned
          command:
            - /bin/sh
            - -ec
            - |
              f="$(ls /backups/*.dump | tail -1)"
              echo "checking $f"
              pg_restore --list "$f" > /tmp/toc
              grep -q LiteLLM_VerificationToken /tmp/toc && echo RESTORABLE
          volumeMounts:
            - name: backups
              mountPath: /backups
      volumes:
        - name: backups
          persistentVolumeClaim:
            claimName: litellm-backups
EOF
"${k[@]}" wait --for=condition=complete job/backup-check --timeout=120s || true
check="$("${k[@]}" logs job/backup-check)"
echo "$check"
grep -q RESTORABLE <<<"$check" || { echo "FAIL backup is not restorable"; exit 1; }

echo "KIND E2E: PASS"
