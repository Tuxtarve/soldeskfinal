apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ticketing
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: ${repo_url}
    targetRevision: ${target_revision}
    path: k8s
  destination:
    server: https://kubernetes.default.svc
    namespace: ticketing
  # sqs-access-sa 는 k8s/_runtime/ 으로 이동해 ArgoCD 관리 밖에서 post_apply_k8s_bootstrap.sh
  # 가 직접 apply. ignoreDifferences 를 쓰지 않아도 됨.
  #
  # HPA/KEDA 가 관리하는 필드(Deployment.replicas, ScaledObject paused annotation 등)는
  # ArgoCD 가 건드리지 않도록 제외 — scale-out 후 git YAML 로 되돌리는 사고 방지.
  #
  # ticketing-config CM 의 "런타임 비상 스위치" 키들도 여기서 제외. 장애 대응 시
  #   kubectl -n ticketing edit cm ticketing-config     # 값 변경
  #   kubectl -n ticketing rollout restart deploy/read-api deploy/write-api deploy/worker-svc ...
  # 순서로 즉시 반영. CM 만 바꿔도 파드는 env 를 재로드하지 않으므로 rollout restart 필수.
  # 장애 종료 후에는 git 에도 같은 값을 반영해 drift 를 정리할 것.
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas
    - group: autoscaling
      kind: HorizontalPodAutoscaler
      jsonPointers:
        - /spec/minReplicas
        - /spec/maxReplicas
    - group: keda.sh
      kind: ScaledObject
      jsonPointers:
        - /spec/minReplicaCount
        - /spec/maxReplicaCount
      jqPathExpressions:
        - '.metadata.annotations."autoscaling.keda.sh/paused"'
    - group: ""
      kind: ConfigMap
      name: ticketing-config
      jsonPointers:
        - /data/CACHE_ENABLED
        - /data/WORKER_DB_MAX_CONCURRENT
        - /data/WORKER_SQS_BATCH_CONCURRENCY
        - /data/CONCERTS_LIST_CACHE_TTL_SEC
        - /data/CONCERT_DETAIL_CACHE_TTL_SEC
        - /data/CONCERT_SEAT_HOLD_TTL_SEC
        - /data/REMAIN_DB_SYNC_ENABLED
  syncPolicy:
    automated:
      prune: true
      selfHeal: true  # git을 단일 진실로 강제 — 수동 kubectl 변경은 수 초 내 원복
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
