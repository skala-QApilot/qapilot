"""run dcf265f7 심층 검증 후속 — 실질 e2e QA 보완 3종 회귀 테스트.

1. 가드 리다이렉트 복구: navigate 가 /login 으로 튕기면 test_account 로 로그인
   후 원 URL 재네비 (TS-004+ 전 TC 가 로그인 화면에 멈추던 원인).
2. 공허한 pass 강등: MANUAL_REVIEW step 증발 후 wait/navigate 만으로 pass 금지.
   (pipeline 인라인 — e2e 재실행으로 검증, 여기선 1·3 만 단위 테스트)
3. signup email per-run 유니크화: 이전 run 의 가입 데이터로 인한 409 연쇄 차단.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.orchestrator.pipeline import _uniquify_signup_email_for_run
from qapilot.tools.ui_test_tool import UITestTool


def _tool(account=None) -> UITestTool:
    tool = UITestTool.__new__(UITestTool)
    tool.logger = MagicMock()
    tool._test_account = account
    return tool


class TestRecoverLoginRedirect:
    @pytest.mark.asyncio
    async def test_redirected_to_login_triggers_auth_and_renav(self):
        account = {"email": "demo1@minibss.test", "password": "pw"}
        tool = _tool(account)
        tool._ensure_authenticated = AsyncMock()

        page = MagicMock()
        urls = iter([
            "http://sut/login",      # 리다이렉트 감지
            "http://sut/dashboard",  # 로그인 후
        ])
        type(page).url = property(lambda self_: next(urls, "http://sut/dashboard"))
        page.goto = AsyncMock()

        await tool._recover_login_redirect(page, "http://sut/dashboard")
        tool._ensure_authenticated.assert_awaited_once()
        page.goto.assert_awaited()  # 원 의도 URL 재네비

    @pytest.mark.asyncio
    async def test_no_account_skips(self):
        tool = _tool(None)
        tool._ensure_authenticated = AsyncMock()
        page = MagicMock()
        page.url = "http://sut/login"
        await tool._recover_login_redirect(page, "http://sut/dashboard")
        tool._ensure_authenticated.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_intended_login_page_skips(self):
        tool = _tool({"email": "a", "password": "b"})
        tool._ensure_authenticated = AsyncMock()
        page = MagicMock()
        page.url = "http://sut/login"
        await tool._recover_login_redirect(page, "http://sut/login")
        tool._ensure_authenticated.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_redirected_skips(self):
        tool = _tool({"email": "a", "password": "b"})
        tool._ensure_authenticated = AsyncMock()
        page = MagicMock()
        page.url = "http://sut/plans"
        await tool._recover_login_redirect(page, "http://sut/plans")
        tool._ensure_authenticated.assert_not_awaited()


class TestUniquifySignupEmail:
    def _mapping(self, email="newuser@test.com"):
        return {
            "tc_id": "TS-001-TC-01",
            "steps": [
                {"action": "navigate", "value": "/signup", "api_endpoint": None},
                {"action": "fill", "selector": "email", "target_name": "email",
                 "value": email, "api_endpoint": None},
                {"action": "click", "selector": "signup-submit",
                 "api_endpoint": "POST /api/auth/signup"},
            ],
        }

    def test_positive_signup_email_gets_run_suffix(self):
        m = self._mapping()
        _uniquify_signup_email_for_run(m, "회원가입이 성공한다", "abcd1234-ef")
        assert m["steps"][1]["value"] == "newuser+abcd1234@test.com"

    def test_negative_intent_untouched(self):
        m = self._mapping()
        _uniquify_signup_email_for_run(m, "이미 존재하는 이메일 오류 (409)", "abcd1234-ef")
        assert m["steps"][1]["value"] == "newuser@test.com"

    def test_non_signup_mapping_untouched(self):
        m = self._mapping()
        m["steps"][2]["api_endpoint"] = "POST /api/auth/login"
        _uniquify_signup_email_for_run(m, "로그인 성공", "abcd1234-ef")
        assert m["steps"][1]["value"] == "newuser@test.com"

    def test_invalid_email_value_untouched(self):
        m = self._mapping(email="invalid-email")
        _uniquify_signup_email_for_run(m, "성공한다", "abcd1234-ef")
        assert m["steps"][1]["value"] == "invalid-email"

    def test_idempotent(self):
        m = self._mapping()
        _uniquify_signup_email_for_run(m, "성공", "abcd1234-ef")
        once = m["steps"][1]["value"]
        _uniquify_signup_email_for_run(m, "성공", "abcd1234-ef")
        assert m["steps"][1]["value"] == once
