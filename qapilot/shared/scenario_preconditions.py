"""시나리오 생성 후처리 — 각 TC 의 `given` 절을 보고 DB precondition
(db_check_sql/db_seed_sql)을 규칙 기반으로 채운다.

독립 실행을 위해 "실행 전 DB 상태 확인 → 미충족 시 시드" 가 가능하도록 만든다.
LLM 비의존(결정적·안정적) — _save_scenarios 후처리에서 1회 적용되어 재생성에도 포함된다.

규칙/SQL 은 현재 SUT(minibss) 스키마 기준(customers/orders 중심). 다른 SUT 로 전환 시
본 모듈의 템플릿/토큰만 교체하면 된다. 이미 값이 있는(수동 작성) TC 는 보존한다.
"""

from __future__ import annotations

from typing import Any

# bcrypt('Passw0rd!', rounds=12) — demo 계정 seed 가 실제 실행될 때(고객 부재 시) 로그인 가능하도록.
_HASH = "$2b$12$zYNlhCkgH0huogGxPxYW8uFiaPPMehPkY2TIIn819AViEnQ9ZChqG"

_CUSTOMER = (
    "SELECT 1 FROM customers WHERE id = 1",
    "INSERT INTO customers (id, email, password_hash, name) "
    f"VALUES (1, 'demo1@minibss.test', '{_HASH}', '데모유저1') ON CONFLICT (id) DO NOTHING",
)
_CUSTOMER2 = (
    "SELECT 1 FROM customers WHERE id = 2",
    "INSERT INTO customers (id, email, password_hash, name) "
    f"VALUES (2, 'demo2@minibss.test', '{_HASH}', '데모유저2') ON CONFLICT (id) DO NOTHING",
)
_ORDER = (
    "SELECT 1 FROM orders WHERE customer_id = 1",
    "INSERT INTO orders (customer_id, status) VALUES (1, 'CONFIRMED')",
)

# 데이터 precondition 이 없는(또는 표현 불가한) given — 회원가입(신규 생성)·인증 음성·토큰류.
_SKIP_TOKENS = (
    "회원가입", "비로그인", "토큰이 없", "토큰을 전달하지 않",
    "위조", "만료", "존재하지 않는 이메일", "미인증", "로그인하지 않",
)


def _precondition_for(given: str) -> tuple[str, str] | None:
    """given 절 → (check_sql, seed_sql) 또는 None(precondition 없음)."""
    g = given or ""
    if any(t in g for t in _SKIP_TOKENS):
        return None
    if "타인" in g or "다른 고객" in g:
        return _CUSTOMER2
    if any(t in g for t in ("구독 중", "이용 중", "보유", "활성 회선")):
        return _ORDER
    if any(t in g for t in ("로그인", "인증된", "인증 사용자", "본인 계정", "인증", "가족 그룹에 소속")):
        return _CUSTOMER
    return None


def apply_preconditions(scenarios: list[Any]) -> int:
    """각 시나리오의 TC dict 에 db_check_sql/db_seed_sql 을 규칙 기반으로 채운다.

    이미 값이 있는 TC 는 보존(수동 작성 우선). 반환: 새로 채운 TC 수.
    """
    applied = 0
    for ts in scenarios or []:
        tcs = ts.get("test_cases") if isinstance(ts, dict) else getattr(ts, "test_cases", None)
        for tc in tcs or []:
            if not isinstance(tc, dict):
                continue
            if tc.get("db_check_sql") or tc.get("db_seed_sql"):
                continue  # 수동/기존 값 보존
            pc = _precondition_for(tc.get("given", ""))
            if pc:
                tc["db_check_sql"], tc["db_seed_sql"] = pc
                applied += 1
    return applied
