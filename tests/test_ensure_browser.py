"""_ensure_browser 헬퍼 단위 테스트.

`is_chromium_installed` / `ensure_chromium` / `ensure_chromium_for_test`:
- 이미 설치된 경우 즉시 True 반환 (subprocess 호출 X)
- 부재 + prompt=False → 자동 다운로드 시도
- 부재 + prompt=True 거절 → False 반환
- 다운로드 실패 → False
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from qapilot.cli import _ensure_browser as EB


def test_ensure_chromium_returns_true_when_already_installed():
    with patch.object(EB, "is_chromium_installed", return_value=True):
        with patch.object(EB, "_run_playwright_install") as mock_install:
            assert EB.ensure_chromium() is True
            mock_install.assert_not_called()


def test_ensure_chromium_prompt_yes_runs_install():
    with patch.object(EB, "is_chromium_installed", return_value=False):
        with patch("qapilot.cli._ensure_browser.Confirm.ask", return_value=True):
            with patch.object(EB, "_run_playwright_install", return_value=True) as install:
                assert EB.ensure_chromium(prompt=True) is True
                install.assert_called_once()


def test_ensure_chromium_prompt_no_skips_install():
    with patch.object(EB, "is_chromium_installed", return_value=False):
        with patch("qapilot.cli._ensure_browser.Confirm.ask", return_value=False):
            with patch.object(EB, "_run_playwright_install") as install:
                assert EB.ensure_chromium(prompt=True) is False
                install.assert_not_called()


def test_ensure_chromium_no_prompt_runs_install():
    """prompt=False (test 진입 경로) 는 사용자 확인 없이 즉시 다운로드."""
    with patch.object(EB, "is_chromium_installed", return_value=False):
        with patch.object(EB, "_run_playwright_install", return_value=True) as install:
            assert EB.ensure_chromium(prompt=False) is True
            install.assert_called_once()


def test_ensure_chromium_install_failure_returns_false():
    with patch.object(EB, "is_chromium_installed", return_value=False):
        with patch.object(EB, "_run_playwright_install", return_value=False):
            assert EB.ensure_chromium(prompt=False) is False


def test_run_playwright_install_success_exit_zero():
    """subprocess exit=0 → True."""
    mock_proc = MagicMock()
    mock_proc.stdout = iter(["downloading chromium...", "done"])
    mock_proc.wait.return_value = 0
    with patch("qapilot.cli._ensure_browser.subprocess.Popen", return_value=mock_proc):
        console = MagicMock()
        assert EB._run_playwright_install(console) is True


def test_run_playwright_install_nonzero_exit_returns_false():
    mock_proc = MagicMock()
    mock_proc.stdout = iter(["error: network unreachable"])
    mock_proc.wait.return_value = 1
    with patch("qapilot.cli._ensure_browser.subprocess.Popen", return_value=mock_proc):
        console = MagicMock()
        assert EB._run_playwright_install(console) is False


def test_run_playwright_install_file_not_found():
    with patch(
        "qapilot.cli._ensure_browser.subprocess.Popen",
        side_effect=FileNotFoundError,
    ):
        console = MagicMock()
        assert EB._run_playwright_install(console) is False


def test_ensure_chromium_for_test_delegates_without_prompt():
    """test 진입 경로는 prompt=False 로 위임."""
    with patch.object(EB, "ensure_chromium", return_value=True) as ensure:
        assert EB.ensure_chromium_for_test() is True
        ensure.assert_called_once_with(prompt=False)
