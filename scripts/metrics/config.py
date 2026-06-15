"""측정 구성 — SUT 상태 매트릭스 · 반복 N · 경로.

측정 설계서(docs/metrics/measurement-design.md) §2·§4 와 정합.
정책 버전은 v3 고정(약관 v3.0). 신뢰 측정 subset = clean + 5 fault.
DATA-001/INFRA-001 은 비결정성으로 초기 제외(설계서 §1.1).
"""
from __future__ import annotations

import os
from pathlib import Path

# ── 경로 ────────────────────────────────────────────────────────────
# 이 파일 기준으로 레포 루트들을 유도 (qapilot/scripts/metrics/config.py)
_QAPILOT_ROOT = Path(__file__).resolve().parents[2]          # .../qapilot
_PROJECT_ROOT = _QAPILOT_ROOT.parent                          # .../last_project
SUT_ROOT = Path(os.getenv("MEASURE_SUT_ROOT", _PROJECT_ROOT / "mini-bss-lite"))
GOLDEN_DIR = SUT_ROOT / "golden" / "scenarios"
FAULTS_DIR = SUT_ROOT / "faults"
OUT_DIR = Path(os.getenv("MEASURE_OUT_DIR", _QAPILOT_ROOT / ".qapilot" / "metrics"))

# ── SUT 접속 (로컬 docker-compose) ──────────────────────────────────
STAGING_URL = os.getenv("MEASURE_STAGING_URL", "http://localhost:3000")   # frontend (QApilot 대상)
BACKEND_URL = os.getenv("MEASURE_BACKEND_URL", "http://localhost:8000")
CONTRACTS_URL = os.getenv("MEASURE_CONTRACTS_URL", "http://localhost:8001")
HEALTH_URLS = [f"{BACKEND_URL}/health", f"{CONTRACTS_URL}/health", STAGING_URL]

# SUT 로그인 (seed 계정 — 성인). 자격검증·약정 흐름에 사용.
TEST_ACCOUNT = {
    "email": os.getenv("MEASURE_SUT_EMAIL", "demo1@minibss.test"),
    "password": os.getenv("MEASURE_SUT_PASSWORD", "Passw0rd!"),
}

# ── 측정 매트릭스 ───────────────────────────────────────────────────
# clean = 결함 OFF. 각 fault = 단일 결함 ON (ENABLED_FAULTS).
CLEAN = "clean"
RELIABLE_FAULTS = [
    "BUG-RULE-001",   # backend  POST /api/auth/signup        (미성년 동의 우회)
    "BUG-RULE-002",   # backend  PATCH .../change-plan        (미성년 일반요금제 우회)
    "BUG-RULE-003",   # contracts PATCH .../terminate         (상위변경 위약금 면제 우회)
    "BUG-RULE-004",   # backend  POST /api/orders             (성인 청소년요금제 제한 우회)
    "BUG-API-001",    # contracts POST /api/contracts         (중복약정 409→500)
]
# 결정성 확보 후 편입 (현재 측정 제외):
EXCLUDED_FAULTS = ["BUG-DATA-001", "BUG-INFRA-001"]

# 측정 단위 = clean + 신뢰 fault. (= 6 구성)
CONFIGS = [CLEAN] + RELIABLE_FAULTS

# ── 반복 (비결정성) ─────────────────────────────────────────────────
N_RUNS = int(os.getenv("MEASURE_N_RUNS", "3"))    # 설계서 §4: 3 기본(5 권장)

# ── 결함 트리거를 위해 ENABLED_FAULTS 에 넣을 값 ────────────────────
def enabled_faults_env(config: str) -> str:
    """구성명 → ENABLED_FAULTS 환경변수 값. clean 은 빈 문자열."""
    return "" if config == CLEAN else config
