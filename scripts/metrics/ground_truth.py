"""Ground truth 로더 — golden(정상 오라클) + faults(주입 결함 정답).

측정 설계서 §1. 두 출처:
- golden/scenarios/GT-*.json : clean SUT 에서 기대 verdict (전 케이스 PASS) + req_id.
- faults/BUG-*.yaml          : 결함별 endpoint·category(①장애유형)·fix(원인 위치).

affected_scenarios 는 추상 TS1/2/3 라벨이라 golden TC 와 직접 매핑 불가 →
**trigger.endpoint** 를 결함↔동작 매핑의 1차 키로 사용한다.
"""
from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# faults/ category(①장애유형 라벨) → defects.defect_type 코드.
# defect_writer._CATEGORY_PREFIX 와 동일 규약(설계서 §3.4).
CATEGORY_TO_DEFECT_TYPE = {
    "RULE": "DOMAIN_RULE",
    "DOMAIN": "DOMAIN_RULE",
    "API": "API_ERROR",
    "DATA": "DATA_MISMATCH",
    "DB": "DATA_MISMATCH",
    "INFRA": "INFRA",
    "UI": "UI_ERROR",
}


@dataclass
class FaultGT:
    """주입 결함 한 건의 정답 메타."""
    id: str
    category: str                 # faults/ 원본 (RULE/API/DATA/INFRA)
    defect_type: str              # ①장애유형 (DOMAIN_RULE/API_ERROR/...)
    severity: str
    endpoint: str                 # trigger.endpoint (정규화된 METHOD PATH)
    method: str
    path: str
    fix_file: str                 # 정답 원인 파일 (Top-N 위치 일치용)
    fix_function: str
    root_cause: str               # 의미 일치(LLM-judge)용 정답 서술
    violated_requirement: str
    affected_scenarios: list[str] = field(default_factory=list)


@dataclass
class GoldenCase:
    """golden TC 한 건 (clean 오라클)."""
    tc_id: str
    req_id: str
    name: str
    then: str
    expected_status: int | None   # then 텍스트에서 파싱한 HTTP 코드
    tags: list[str]


_STATUS_RE = re.compile(r"HTTP\s*(\d{3})")


def _yaml_load(path: str) -> dict:
    """pyyaml 있으면 사용, 없으면 최소 파서 회피용 import 실패 시 예외."""
    import yaml  # pyyaml (SUT 개발 의존성에 포함)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_faults(faults_dir: str | Path) -> dict[str, FaultGT]:
    """faults/BUG-*.yaml → {fault_id: FaultGT}."""
    out: dict[str, FaultGT] = {}
    for fp in sorted(glob.glob(str(Path(faults_dir) / "BUG-*.yaml"))):
        d = _yaml_load(fp)
        cat = (d.get("category") or "").upper()
        endpoint = (d.get("trigger", {}) or {}).get("endpoint", "") or ""
        method, path = _split_endpoint(endpoint)
        fix = d.get("fix_recommendation", {}) or {}
        out[d["id"]] = FaultGT(
            id=d["id"],
            category=cat,
            defect_type=CATEGORY_TO_DEFECT_TYPE.get(cat, "UI_ERROR"),
            severity=d.get("severity", ""),
            endpoint=endpoint.strip(),
            method=method,
            path=path,
            fix_file=fix.get("file", "") or "",
            fix_function=fix.get("function", "") or "",
            root_cause=str(d.get("root_cause", "") or "").strip(),
            violated_requirement=str(d.get("violated_requirement", "") or ""),
            affected_scenarios=list(d.get("affected_scenarios", []) or []),
        )
    return out


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    """'PATCH /api/contracts/{order_id}/terminate' → ('PATCH', '/api/contracts/{order_id}/terminate')."""
    parts = (endpoint or "").strip().split(None, 1)
    if len(parts) == 2 and parts[0].isupper():
        return parts[0], parts[1]
    return "", endpoint.strip()


def load_golden(golden_dir: str | Path) -> dict[str, GoldenCase]:
    """golden/scenarios/GT-*.json → {tc_id: GoldenCase}."""
    out: dict[str, GoldenCase] = {}
    for fp in sorted(glob.glob(str(Path(golden_dir) / "GT-*.json"))):
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        for tc in d.get("test_cases", []):
            then = tc.get("then", "") or ""
            m = _STATUS_RE.search(then)
            out[tc["tc_id"]] = GoldenCase(
                tc_id=tc["tc_id"],
                req_id=tc.get("req_id", "") or "",
                name=tc.get("name", "") or "",
                then=then,
                expected_status=int(m.group(1)) if m else None,
                tags=list(tc.get("tags", []) or []),
            )
    return out


# ── 결함 ↔ 동작(endpoint) 매핑 ──────────────────────────────────────
def fault_path_regex(fault: FaultGT) -> re.Pattern:
    """결함 endpoint path 를 {param} 무시한 정규식으로. TC 가 해당 엔드포인트를
    호출했는지 판정용 (action_mapping 의 api_endpoint 와 매칭)."""
    # '/api/contracts/{order_id}/terminate' → '/api/contracts/[^/]+/terminate'
    esc = re.escape(fault.path)
    esc = re.sub(r"\\\{[^/}]+\\\}", r"[^/]+", esc)
    return re.compile(esc + r"/?$")
