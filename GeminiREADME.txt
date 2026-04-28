==========================================================
 Gemini AI Advisor — 추천 결과 조회 가이드
==========================================================

[이 가이드가 하는 일]
EKS 메트릭을 10분마다 자동 수집해 Gemini AI 가 오토스케일링을
추천하고 그 결과를 확인하는 방법을 안내합니다.

[추천 결과 저장 위치]
  1. GCP Cloud Logging  → 이력 영구 보관 (브라우저)
  2. 로컬 파일          → scripts/data/recommendation-*.json
  3. EKS CronJob 로그   → kubectl logs
  4. 터미널 즉시 출력   → 수동 실행


==========================================================
 1. GCP Cloud Logging (브라우저) — 이력 전체 조회
==========================================================

접속: https://console.cloud.google.com/logs/query
프로젝트: soldesk-gcp

──────────────────────────────────────────────────────────
[1-A] Gemini 추천 결과 조회
──────────────────────────────────────────────────────────
Logs Explorer 쿼리창에 아래 입력 후 실행:

    logName="projects/soldesk-gcp/logs/gemini-recommendations"

조회되는 항목:
  - summary       : 전체 요약 한 줄
  - recommendations: 서비스별 추천 (target / field / from → to)
  - estimatedCostDelta: 예상 비용 증감
  - warnings      : 주의 사항
  - openQuestions : 추가 확인 필요 항목

심각도 분류:
  WARNING → risk:high 항목 포함 (즉각 확인 필요)
  NOTICE  → priority:now 항목 포함 (당일 적용 권고)
  INFO    → 일반 최적화 제안

──────────────────────────────────────────────────────────
[1-B] EKS 수집 메트릭 원본 조회
──────────────────────────────────────────────────────────
    logName="projects/soldesk-gcp/logs/eks-metrics"

조회되는 항목:
  - SQS backlog / in-flight
  - RDS 커넥션 수 / CPU
  - Redis 메모리 / eviction
  - HPA current/desired replicas
  - KEDA ScaledObject 상태
  - Node CPU / Memory
  - Pod 재시작 횟수


==========================================================
 2. 로컬 파일 — scripts/data/
==========================================================

CronJob 또는 수동 실행 시 로컬에 파일이 저장됩니다.
scripts/data/ 는 .gitignore 로 제외되어 git에 올라가지 않습니다.

──────────────────────────────────────────────────────────
[2-A] 저장된 파일 목록 확인
──────────────────────────────────────────────────────────
    ls -lt ~/soldeskfinal/scripts/data/

파일 종류:
  metrics-YYYYMMDD-HHMMSS.json      ← 수집된 EKS 메트릭
  recommendation-YYYYMMDD-HHMMSS.json ← Gemini 추천 결과
  patches-YYYYMMDD-HHMMSS/          ← kubectl 패치 파일 디렉터리
    ├── README.md                   ← 적용 순서 안내
    ├── 00-hpa-read-api-hpa.yaml   ← 즉시 적용 가능한 K8s YAML
    ├── 01-hpa-write-api-hpa.yaml
    └── manual-actions.md           ← YAML로 표현 불가한 수동 조치 목록
  threshold-<mode>-<ts>.json       ← 이분탐색 임계값 탐색 결과
  pre-scale-<ts>.json              ← 사전 스케일링 추천 결과
  monitor-latest.txt               ← 실시간 모니터 최신 스냅샷

──────────────────────────────────────────────────────────
[2-B] 최신 추천 결과 바로 보기
──────────────────────────────────────────────────────────
    cat $(ls -t ~/soldeskfinal/scripts/data/recommendation-*.json \
      | head -1) | python3 -m json.tool | less

요약만 빠르게 보기:
    python3 -c "
    import json, glob, os
    f = sorted(glob.glob('scripts/data/recommendation-*.json'))[-1]
    d = json.load(open(f))
    print('파일:', os.path.basename(f))
    print('요약:', d['summary'])
    print()
    for r in d['recommendations']:
        print(f\"  [{r['priority'].upper()}/{r['risk']}] {r['target']}\"
              f\" {r['field']}: {r['from']} → {r['to']}\")
        print(f\"    → {r['reason'][:80]}\")
    "

──────────────────────────────────────────────────────────
[2-C] 패치 파일 내용 확인
──────────────────────────────────────────────────────────
    LATEST=$(ls -dt ~/soldeskfinal/scripts/data/patches-* | head -1)

    # 적용 순서 및 요약
    cat "$LATEST/README.md"

    # 실제 패치 내용
    cat "$LATEST/00-hpa-read-api-hpa.yaml"

    # 수동 조치 목록 (RDS pool, Redis 등)
    cat "$LATEST/manual-actions.md"


==========================================================
 3. EKS CronJob 로그 — 실시간 실행 결과
==========================================================

──────────────────────────────────────────────────────────
[3-A] 최근 CronJob 실행 로그 확인
──────────────────────────────────────────────────────────
    kubectl logs -n ticketing -l app=ai-advisor --tail=50

출력 예시:
  [*] AI Advisor CronJob 시작: 2026-04-28T02:00:00Z
  [*] k8s 리소스 수집...
  [*] Prometheus 수집 중...
  [*] CloudWatch 수집 중...
  [+] GCP 전송 완료: eks-metrics [INFO]
  [*] Gemini 추천 요청...
  [+] 추천 수신: NOW 2건
  [+] GCP 전송 완료: gemini-recommendations [NOTICE]
  [*] AI Advisor 완료

──────────────────────────────────────────────────────────
[3-B] 즉시 실행 테스트 (10분 기다리지 않고 바로)
──────────────────────────────────────────────────────────
    kubectl delete job ai-advisor-now -n ticketing \
      --ignore-not-found 2>/dev/null
    kubectl create job ai-advisor-now \
      --from=cronjob/ai-advisor -n ticketing

    # 로그 스트리밍
    kubectl logs -n ticketing -l job-name=ai-advisor-now -f

──────────────────────────────────────────────────────────
[3-C] CronJob 실행 이력 확인
──────────────────────────────────────────────────────────
    kubectl get cronjob ai-advisor -n ticketing
    kubectl get jobs -n ticketing | grep ai-advisor


==========================================================
 4. 터미널 즉시 실행 — 수동으로 바로 받기
==========================================================

──────────────────────────────────────────────────────────
[4-A] 전체 파이프라인 한 방 실행
──────────────────────────────────────────────────────────
    cd ~/soldeskfinal
    source .env.local

    python3 scripts/collect_metrics.py && \
    python3 scripts/recommend_scaling.py && \
    python3 scripts/recommendation_to_patches.py && \
    python3 scripts/push_to_cloud_logging.py && \
    python3 scripts/notify.py

각 단계 설명:
  collect_metrics.py          → EKS/SQS/CloudWatch/Prometheus 수집
  recommend_scaling.py        → Gemini 추천 (터미널에 표 형식 출력)
  recommendation_to_patches.py→ K8s YAML 패치 파일 생성
  push_to_cloud_logging.py    → GCP Cloud Logging 저장
  notify.py                   → Slack 알림 (priority:now만)

──────────────────────────────────────────────────────────
[4-B] 추천 결과 터미널 출력 형식
──────────────────────────────────────────────────────────
python3 scripts/recommend_scaling.py 실행 시 아래 형식으로 출력됩니다:

  === Gemini 추천 요약 ===
  요약: HPA maxReplicas 조정 및 KEDA queueLength 최적화 권고

  PRI    RISK  CONF   TARGET                  FIELD            FROM → TO
  -----------------------------------------------------------------------
  NOW    low   high   hpa/read-api-hpa        spec.maxReplicas 23 → 10
         ↳ 평시 RPS 5 기준 10개면 충분, 23은 과도한 상한
  WATCH  ·     medium scaledobject/worker-svc spec.queueLength 1 → 5
         ↳ 메시지 1개부터 즉시 스케일 → 불필요한 scale-up 방지
  LATER  ·     low    deployment/read-api     resources.cpu    200m → 100m
         ↳ 실측 CPU 3m, requests 저평가 해소

  비용 영향: -$8.00/월 — maxReplicas 축소로 불필요한 Pod 방지

  추가로 확인 필요:
    - 실측 p95 latency 수치 필요
    - RDS pool_size 설정값 확인

──────────────────────────────────────────────────────────
[4-C] 실시간 메트릭 모니터링
──────────────────────────────────────────────────────────
    source .env.local

    # 세분화 메트릭 1회 출력
    python3 scripts/realtime_monitor.py --once

    # 15초 간격 반복 출력
    python3 scripts/realtime_monitor.py

    # 자동 스케일링 포함
    python3 scripts/realtime_monitor.py --auto

    # 파일 저장 (scripts/data/monitor-latest.txt)
    python3 scripts/realtime_monitor.py --file


==========================================================
 5. 패치 파일 적용 순서
==========================================================

Gemini 추천을 실제 클러스터에 적용할 때는 반드시 아래 순서로 진행하세요.

    LATEST=$(ls -dt ~/soldeskfinal/scripts/data/patches-* | head -1)

STEP 1 — 서버 검증 (API 서버가 수락하는지 확인)
    kubectl apply -n ticketing --dry-run=server \
      -f "$LATEST/00-hpa-read-api-hpa.yaml"
  → 오류 없으면 다음 단계 진행

STEP 2 — diff 확인 ⭐ 강추 (현재값 vs 변경될 값)
    kubectl diff -n ticketing \
      -f "$LATEST/00-hpa-read-api-hpa.yaml"
  → "-" 현재값, "+" 변경될 값 확인 후 이상 없으면 진행

STEP 3 — 실제 적용
    kubectl apply -n ticketing \
      -f "$LATEST/00-hpa-read-api-hpa.yaml"

STEP 4 — 적용 결과 확인
    kubectl get hpa -n ticketing
    kubectl get scaledobject -n ticketing
    kubectl describe hpa read-api-hpa -n ticketing


==========================================================
 6. 사전 스케일링 (티켓팅 오픈 전)
==========================================================

이벤트 오픈 N분 전 Gemini가 예상 접속자 수를 분석해 Pod를 미리 증설합니다.

    source .env.local

    # 12:00 오픈, 5000명 예상 — 검토용 (DRY-RUN)
    python3 scripts/pre_scale.py \
      --event-time 12:00 \
      --event-name "콘서트 7회차" \
      --expected-users 5000

    # 실제 자동 적용 (오픈 10분 전 증설 + 30분 후 복구)
    python3 scripts/pre_scale.py \
      --event-time 12:00 \
      --event-name "콘서트 7회차" \
      --expected-users 5000 \
      --auto

  → 결과: scripts/data/pre-scale-<ts>.json


==========================================================
 7. 임계값 탐색 (부하 테스트)
==========================================================

Gemini가 이분 탐색으로 장애 직전 최대 부하 임계값을 자동 탐색합니다.

    source .env.local

    # KEDA 임계값 (SQS 메시지 수 기준)
    python3 scripts/find_threshold.py --mode keda

    # HPA 임계값 (HTTP RPS 기준)
    python3 scripts/find_threshold.py --mode hpa-read
    python3 scripts/find_threshold.py --mode hpa-write

    # 현재 상태만 분석 (부하 없음, 비용 0)
    python3 scripts/find_threshold.py --mode keda --dry-run

  → 결과: scripts/data/threshold-<mode>-<ts>.json
  → 약 20분, 10회 반복 후 임계값 + HPA/KEDA 권장 설정값 출력


==========================================================
 8. 데이터 정리
==========================================================

scripts/data/ 는 .gitignore 로 제외되어 있습니다.
파일이 누적되므로 주기적으로 정리하세요.

    # 파일 목록 및 용량 확인
    ls -lh ~/soldeskfinal/scripts/data/
    du -sh ~/soldeskfinal/scripts/data/

    # 오래된 파일 삭제 (7일 이상)
    find ~/soldeskfinal/scripts/data/ -name "*.json" -mtime +7 -delete
    find ~/soldeskfinal/scripts/data/ -name "*.txt"  -mtime +7 -delete

    # 패치 디렉터리 전체 삭제
    rm -rf ~/soldeskfinal/scripts/data/patches-*/
