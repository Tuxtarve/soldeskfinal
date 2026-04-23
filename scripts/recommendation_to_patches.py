#!/usr/bin/env python3
"""Gemini 추천 JSON → Kustomize strategic-merge-patch YAML 파일 생성.

입력:
  scripts/data/recommendation-*.json  (최신 자동 선택 또는 인자로 경로)

출력:
  scripts/data/patches-<ts>/
    ├── README.md              (무엇을 어떻게 적용할지)
    ├── 00-hpa-read-api.yaml   (타겟별 패치 파일)
    ├── 01-hpa-write-api.yaml
    └── ...
    └── manual-actions.md      (YAML 로 표현 불가능한 추천: RDS pool, Redis, 노드 믹스)

사용:
  python3 scripts/recommendation_to_patches.py
  python3 scripts/recommendation_to_patches.py scripts/data/recommendation-xxxx.json

적용(검토 후 수동):
  kubectl patch -n ticketing -f scripts/data/patches-<ts>/00-hpa-read-api.yaml ...
  또는 kustomize overlay 에 복사
"""
from __future__ import annotations

import glob
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
NS = "ticketing"

# 타겟 kind → apiVersion 매핑
KIND_MAP = {
    "hpa":          ("autoscaling/v2",     "HorizontalPodAutoscaler"),
    "deployment":   ("apps/v1",            "Deployment"),
    "scaledobject": ("keda.sh/v1alpha1",   "ScaledObject"),
    "pdb":          ("policy/v1",          "PodDisruptionBudget"),
}

# YAML 로 표현 불가 → manual-actions.md 로 분기
NON_K8S_PREFIXES = ("application/", "redis", "cluster", "eks")


def latest_recommendation() -> Path:
    files = sorted(glob.glob(str(DATA_DIR / "recommendation-*.json")))
    if not files:
        sys.stderr.write("recommendation-*.json 없음. recommend_scaling.py 먼저 실행.\n")
        sys.exit(1)
    return Path(files[-1])


def coerce(v: str):
    """from/to 문자열을 YAML 에 넣기 좋은 타입으로."""
    if v is None or v == "" or v == "없음" or v == "추가":
        return v
    # 숫자
    try:
        if "." not in v:
            return int(v)
        return float(v)
    except ValueError:
        pass
    # 퍼센트 그대로
    return v


def set_nested(d: dict, path: list[str], value) -> None:
    cur = d
    for key in path[:-1]:
        m = re.match(r"^(.+)\[(\d+)\]$", key)
        if m:
            k, idx = m.group(1), int(m.group(2))
            cur = cur.setdefault(k, [])
            while len(cur) <= idx:
                cur.append({})
            cur = cur[idx]
        else:
            cur = cur.setdefault(key, {})
    last = path[-1]
    cur[last] = value


def parse_target(target: str):
    """'hpa/read-api-hpa' → ('hpa', 'read-api-hpa')"""
    if "/" not in target:
        return None, target
    kind, name = target.split("/", 1)
    return kind.lower(), name


def is_non_k8s(target: str) -> bool:
    t = target.lower()
    return any(t.startswith(p) for p in NON_K8S_PREFIXES) or t.startswith("eks ")


# 프로브 기본 템플릿 (추천이 "없음 → 추가" 인 경우)
PROBE_DEFAULTS = {
    "livenessProbe":  {"httpGet": {"path": "/health", "port": "http"},
                        "initialDelaySeconds": 10, "periodSeconds": 10, "failureThreshold": 3},
    "readinessProbe": {"httpGet": {"path": "/health", "port": "http"},
                        "initialDelaySeconds": 5, "periodSeconds": 5, "failureThreshold": 3},
}


def build_patches(rec: dict) -> tuple[dict, list[dict]]:
    """
    returns:
      patches: { (kind, name): patch_dict }
      manual:  [rec, ...]  YAML 불가 항목
    """
    patches: dict[tuple, dict] = {}
    manual: list[dict] = []

    for r in rec.get("recommendations", []):
        target = r["target"]
        if is_non_k8s(target):
            manual.append(r)
            continue

        kind, name = parse_target(target)
        if kind not in KIND_MAP:
            manual.append(r)
            continue

        apiver, k8s_kind = KIND_MAP[kind]
        key = (kind, name)
        patch = patches.setdefault(key, {
            "apiVersion": apiver,
            "kind": k8s_kind,
            "metadata": {"name": name, "namespace": NS},
            "_comments": [],
        })

        field = r["field"]
        to_val = r["to"]

        # "없음 → 추가" 프로브 처리
        m_probe = re.search(r"(livenessProbe|readinessProbe)$", field)
        if m_probe and (r["from"] in ("없음", "") or to_val in ("추가", "")):
            probe_kind = m_probe.group(1)
            # 경로를 containers[0] 까지만
            path_str = field.rsplit(f".{probe_kind}", 1)[0]
            set_nested(patch, path_str.split(".") + [probe_kind], PROBE_DEFAULTS[probe_kind])
            patch["_comments"].append(
                f"# [{r['priority']}/{r['risk']}] {field}: 추가 — {r['reason']}"
            )
            continue

        # 일반 값 설정
        try:
            set_nested(patch, field.split("."), coerce(to_val))
            patch["_comments"].append(
                f"# [{r['priority']}/{r['risk']}] {field}: {r['from']} → {r['to']} — {r['reason']}"
            )
        except Exception as e:
            manual.append({**r, "_error": str(e)})

    return patches, manual


def emit_patch_yaml(out_dir: Path, idx: int, kind: str, name: str, patch: dict) -> Path:
    comments = patch.pop("_comments", [])
    path = out_dir / f"{idx:02d}-{kind}-{name}.yaml"
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# Strategic merge patch for {kind}/{name}\n")
        for c in comments:
            f.write(c + "\n")
        f.write("#\n# apply: kubectl patch -n ticketing " +
                f"{kind} {name} --patch-file {path.name} --type=strategic\n")
        f.write("#   (또는 kustomize overlay 의 patches: 에 포함)\n")
        f.write("---\n")
        yaml.safe_dump(patch, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return path


def emit_readme(out_dir: Path, rec: dict, patches: dict, manual: list) -> None:
    cost = rec.get("estimatedCostDelta") or {}
    lines = [
        f"# Gemini 추천 → Kustomize 패치",
        "",
        f"원본: `{rec.get('_source','(unknown)')}`",
        f"생성: {datetime.utcnow().isoformat()}Z",
        "",
        "## 요약",
        rec.get("summary", ""),
        "",
        f"- 패치 파일: **{len(patches)}개**",
        f"- 수동 조치: **{len(manual)}개** (manual-actions.md)",
        f"- 비용 영향: {cost.get('direction','?')} ~${cost.get('approxUSDPerMonth',0):.2f}/월",
        "",
        "## 패치 파일 (우선순위 NOW 항목 먼저 적용 권장)",
        "",
    ]
    for (kind, name), p in patches.items():
        lines.append(f"- `{kind}-{name}.yaml`")
    lines += [
        "",
        "## 적용 방법 (검토 후)",
        "```bash",
        "# 1) 개별 리소스 dry-run 으로 미리보기",
        "kubectl apply -n ticketing --dry-run=server -f 00-hpa-read-api.yaml",
        "",
        "# 2) 실제 적용",
        "kubectl apply -n ticketing -f 00-hpa-read-api.yaml",
        "",
        "# 3) kustomize overlay 에 포함하는 경우",
        "#    overlays/prod/kustomization.yaml 의 patches: 에 파일 경로 추가",
        "```",
        "",
        "## ⚠️ 경고",
    ]
    for w in rec.get("warnings", []):
        lines.append(f"- {w}")
    lines += ["", "## 추가 확인 필요"]
    for q in rec.get("openQuestions", []):
        lines.append(f"- {q}")
    (out_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def emit_manual(out_dir: Path, manual: list) -> None:
    if not manual:
        return
    lines = ["# YAML 로 표현 불가능한 수동 조치 항목", ""]
    for r in manual:
        lines += [
            f"## [{r.get('priority')}/{r.get('risk')}] {r['target']} — {r['field']}",
            f"- from: `{r['from']}`",
            f"- to:   `{r['to']}`",
            f"- reason: {r['reason']}",
        ]
        if r.get("_error"):
            lines.append(f"- ⚠️ patch 생성 실패: {r['_error']}")
        lines.append("")
    (out_dir / "manual-actions.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_recommendation()
    rec = json.loads(src.read_text(encoding="utf-8"))
    rec["_source"] = str(src)

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_dir = DATA_DIR / f"patches-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    patches, manual = build_patches(rec)

    # Deployment strategic merge 의 merge key 로 container.name 필수.
    # 이 프로젝트에서는 container 이름 = Deployment 이름 (규약) → 자동 주입.
    for (kind, name), patch in patches.items():
        if kind != "deployment":
            continue
        try:
            containers = patch["spec"]["template"]["spec"]["containers"]
            if containers and "name" not in containers[0]:
                containers[0] = {"name": name, **containers[0]}
                patch["spec"]["template"]["spec"]["containers"] = containers
        except (KeyError, TypeError):
            pass

    print(f"[*] source: {src}")
    print(f"[*] output: {out_dir}")

    for i, ((kind, name), patch) in enumerate(patches.items()):
        p = emit_patch_yaml(out_dir, i, kind, name, patch)
        print(f"[+] {p.name}")

    emit_manual(out_dir, manual)
    emit_readme(out_dir, rec, patches, manual)

    print(f"\n패치 {len(patches)}개 · 수동 {len(manual)}개 생성.")
    print(f"다음: less {out_dir}/README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
