#!/usr/bin/env bash
# Operates only on an explicitly selected, already installed disposable test namespace.
set -euo pipefail
: "${KUBE_CONTEXT:?set the exact test cluster context}"
: "${KUBE_NAMESPACE:?set a flowtool-smoke-* namespace}"
: "${FLOWTOOL_K8S_STORAGE_CLASS:?set the expected RWOP CSI StorageClass}"
: "${FLOWTOOL_K8S_RENDERED:?set the rendered, namespace-specific manifest path}"
case "$KUBE_NAMESPACE" in flowtool-smoke-?*) ;; *) echo 'refusing non-test namespace' >&2; exit 2;; esac
python scripts/smoke/validate_kubernetes.py "$FLOWTOOL_K8S_RENDERED"
kubectl_bin=${KUBECTL:-kubectl}
k=("$kubectl_bin" --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE")
"${k[@]}" apply --dry-run=server -f "$FLOWTOOL_K8S_RENDERED" >/dev/null
"${k[@]}" wait --for=condition=complete job/flowtool-schema --timeout=180s
for role in serve worker-1; do
  test "$("${k[@]}" get "statefulset/flowtool-$role" -o jsonpath='{.spec.replicas}')" = 1
  "${k[@]}" wait --for=condition=ready "pod/flowtool-$role-0" --timeout=300s
  test "$("${k[@]}" get pods -l "flowtool-role=$role" -o name | wc -l | tr -d ' ')" = 1
done
pvc=data-flowtool-worker-1-0
test "$("${k[@]}" get "pvc/$pvc" -o jsonpath='{.spec.accessModes[0]}')" = ReadWriteOncePod
test "$("${k[@]}" get "pvc/$pvc" -o jsonpath='{.spec.storageClassName}')" = "$FLOWTOOL_K8S_STORAGE_CLASS"
test "$("${k[@]}" get "pvc/$pvc" -o jsonpath='{.status.phase}')" = Bound
"${k[@]}" exec flowtool-serve-0 -c serve -- python -c \
  "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=3).status==200"
"${k[@]}" exec flowtool-worker-1-0 -- python -c \
  "import os; from pathlib import Path; assert os.getuid()==10001; assert not os.getenv('FLOWTOOL_DATABASE_URL'); assert not Path('/run/secrets/github/app.pem').exists(); assert not Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists()"
"${k[@]}" exec flowtool-worker-1-0 -- python -c \
  "from pathlib import Path; from app.worker.journal import WorkerJournal; r=WorkerJournal(Path('/var/lib/flowtool')).load(); print('journal validated' if r else 'journal absent: no active recovery evidence')"
if [ "${FLOWTOOL_K8S_RECREATE_IDLE_WORKER:-no}" = yes ]; then
  "${k[@]}" exec flowtool-serve-0 -c serve -- flowtool worker-drain worker-1
  row=$("${k[@]}" exec flowtool-serve-0 -c serve -- flowtool workers | awk '$1=="worker-1" {print $2,$3,$4,$5}')
  test "$row" = 'online True - -'
  "${k[@]}" exec flowtool-worker-1-0 -- touch /var/lib/flowtool/.k8s-smoke
  "${k[@]}" delete pod flowtool-worker-1-0 --wait=true --timeout=90s
  "${k[@]}" wait --for=create pod/flowtool-worker-1-0 --timeout=90s
  "${k[@]}" wait --for=condition=ready pod/flowtool-worker-1-0 --timeout=300s
  "${k[@]}" exec flowtool-worker-1-0 -- test -f /var/lib/flowtool/.k8s-smoke
  "${k[@]}" exec flowtool-serve-0 -c serve -- flowtool worker-drain worker-1 --undo
fi
"$kubectl_bin" --context "$KUBE_CONTEXT" version -o json
"$kubectl_bin" --context "$KUBE_CONTEXT" get storageclass "$FLOWTOOL_K8S_STORAGE_CLASS" -o jsonpath='{.provisioner}{"\n"}'
printf 'Idle deployment smoke passed. Active journal remount, node loss and live GitHub/model recovery NOT VALIDATED. Resources retained.\n'
