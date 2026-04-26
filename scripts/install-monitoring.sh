#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Installing kube-prometheus-stack ==="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo update

# namespace 먼저 생성
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

# gp3 StorageClass (EBS CSI 드라이버용)
kubectl apply -f "$ROOT_DIR/monitoring/k8s/storageclass-gp3.yaml"

# Grafana dashboard ConfigMap (Helm보다 먼저 — sidecar가 자동 로드)
kubectl apply -f "$ROOT_DIR/monitoring/k8s/grafana-dashboards-configmap.yaml"

# kube-prometheus-stack (CRD 포함 — PrometheusRule 적용 전에 설치해야 함)
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values "$ROOT_DIR/monitoring/values-kube-prometheus-stack.yaml" \
  --wait --timeout 10m

# PrometheusRule (kube-prometheus-stack이 CRD를 설치한 뒤에 적용)
kubectl apply -f "$ROOT_DIR/monitoring/prometheus-rules.yaml"

echo "=== Installing Loki ==="
helm upgrade --install loki grafana/loki \
  --namespace monitoring \
  --values "$ROOT_DIR/monitoring/values-loki.yaml" \
  --wait --timeout 5m

echo "=== Installing Promtail ==="
# 실사용 10m/40Mi — Burstable QoS + 스케줄링 예측성 확보용 requests 명시.
helm upgrade --install promtail grafana/promtail \
  --namespace monitoring \
  --set "config.clients[0].url=http://loki:3100/loki/api/v1/push" \
  --set "priorityClassName=ticketing-priority-platform" \
  --set "resources.requests.cpu=20m" \
  --set "resources.requests.memory=64Mi" \
  --set "resources.limits.cpu=100m" \
  --set "resources.limits.memory=128Mi" \
  --wait --timeout 5m

# Grafana ALB Ingress
kubectl apply -f "$ROOT_DIR/monitoring/k8s/grafana-ingress.yaml"

echo ""
echo "=== Monitoring stack installed ==="
echo "Grafana : kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80"
echo "          or via ALB at /grafana  (root / soldesk1.)"
echo "Prometheus: kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090"
