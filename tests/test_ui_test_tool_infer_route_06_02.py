"""2026-06-02 진단 — `_infer_target_route` 휴리스틱 v3 (`/auth/` 마지막 segment) 검증.

배경: SaaS 첫 e2e (trace `06b6e958`) 의 TS-001 UI 100% fail 원인 — `POST /api/auth/signup`
이 `/api/` 제거 후 1차 segment `/auth` 반환 → vue-router 라우트 부재 → 빈 main → 흰화면.
mini-bss-lite 실제 라우트는 `/signup`/`/login` 평탄 구조.

v3 보강: 1차 segment 가 `auth` 일 때 마지막 segment 사용. 그 외 케이스는 기존 v2 동작 유지.

상세: memory/project_qapilot_06_02_white_screen_root_cause.md
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from qapilot.tools.ui_test_tool import UITestTool


def _make_tool() -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    return tool


# ── v3 보강 (well-known /auth/) ──


@pytest.mark.parametrize("api_endpoint, expected", [
    # 본 진단 직접 케이스 (TS-001 회원가입/로그인 시나리오)
    ("POST /api/auth/signup", "/signup"),
    ("POST /api/auth/login", "/login"),
    # 그 외 well-known 인증 경로
    ("POST /api/auth/reset-password", "/reset-password"),
    ("POST /api/auth/logout", "/logout"),
    ("GET /api/auth/me", "/me"),
    # `/api/` 없이 `/auth/` 만 있는 경우 (드물지만 가능)
    ("POST /auth/signup", "/signup"),
    ("POST /auth/login", "/login"),
])
def test_infer_target_route_auth_uses_last_segment(api_endpoint, expected):
    """`/auth/{action}` 형식은 backend 그룹 묶음 — frontend route 는 평탄 (마지막 segment)."""
    tool = _make_tool()
    steps = [{"action": "fill", "api_endpoint": api_endpoint}]
    assert tool._infer_target_route(steps) == expected


def test_infer_target_route_auth_single_segment_falls_back():
    """`/auth` 단독 (그 뒤 segment 없음) 은 v3 분기 조건 (len >= 2) 미충족 → 기존 1차 segment 동작."""
    tool = _make_tool()
    steps = [{"action": "fill", "api_endpoint": "GET /auth"}]
    # /auth → segments = ["auth"], len 1 → v3 미적용 → "/auth" 그대로 (1차 segment)
    # mini-bss-lite 같은 실제 SUT 에서 /auth 단독 API 는 없지만, 휴리스틱 robustness 목적.
    assert tool._infer_target_route(steps) == "/auth"


# ── 회귀 — 기존 v2 동작 보존 ──


@pytest.mark.parametrize("api_endpoint, expected", [
    # /api/ 제거 + 1차 segment (기존 v2)
    ("GET /api/plans", "/plans"),
    ("GET /api/plans/{id}", "/plans"),
    ("POST /api/orders", "/orders"),
    ("DELETE /api/contracts/1/cancel", "/contracts"),
    ("GET /api/family-group/join", "/family-group"),
    # /api/ 없는 평탄 경로 (기존 v1)
    ("POST /login", "/login"),
    ("GET /signup", "/signup"),
    ("/plans", "/plans"),
    # `/api` 단독 skip → 다음 endpoint 사용
])
def test_infer_target_route_v2_regression(api_endpoint, expected):
    tool = _make_tool()
    steps = [{"action": "fill", "api_endpoint": api_endpoint}]
    assert tool._infer_target_route(steps) == expected


def test_infer_target_route_auth_among_multiple_steps_uses_first():
    """여러 step 중 첫 발견 endpoint 가 `/api/auth/*` 면 v3 적용."""
    tool = _make_tool()
    steps = [
        {"action": "fill", "api_endpoint": None},
        {"action": "fill", "api_endpoint": None},
        {"action": "click", "api_endpoint": "POST /api/auth/signup"},  # 첫 발견 — v3
        {"action": "assert", "api_endpoint": "GET /api/dashboard"},   # 무시
    ]
    assert tool._infer_target_route(steps) == "/signup"
