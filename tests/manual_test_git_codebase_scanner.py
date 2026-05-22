"""GitCodebaseScannerTool 수동 통합 테스트.

실제 SUT 레포에 접근하여 스캔 결과를 터미널에 출력한다.
실행: python tests/manual_test_git_codebase_scanner.py
"""

import asyncio
import json
import sys
from pathlib import Path

# 패키지 루트를 sys.path에 추가 (개발 환경 대응)
sys.path.insert(0, str(Path(__file__).parent.parent))

from qapilot.shared.schemas import ToolInput
from qapilot.tools.git_codebase_scanner_tool import GitCodebaseScannerTool

_TRIGGERS = ["init", "code_change", "doc_update", "natural_lang"]


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _collect_inputs() -> dict:
    print("\n=== GitCodebaseScannerTool 수동 테스트 ===\n")

    repo_url = _prompt("레포 URL (예: https://github.com/org/repo)")
    if not repo_url:
        print("레포 URL은 필수입니다.")
        sys.exit(1)

    token = _prompt("액세스 토큰")
    branch = _prompt("브랜치", "main")

    print(f"트리거 선택: {', '.join(_TRIGGERS)}")
    trigger = _prompt("트리거", "init")
    if trigger not in _TRIGGERS:
        print(f"유효하지 않은 트리거입니다. 선택지: {_TRIGGERS}")
        sys.exit(1)

    last_commit_hash = ""
    if trigger in ("code_change", "natural_lang"):
        last_commit_hash = _prompt("이전 커밋 해시 (증분 스캔, 없으면 엔터)", "")

    return {
        "repo_url": repo_url,
        "token": token,
        "branch": branch,
        "trigger": trigger,
        "last_commit_hash": last_commit_hash,
    }


def _print_section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _print_scan_result(result: dict) -> None:
    scan_result = result.get("scan_result")
    metadata = result.get("_metadata", {})

    _print_section("메타데이터")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))

    if scan_result is None:
        _print_section("결과: 스킵됨")
        return

    _print_section("언어 / 프레임워크")
    print(f"  언어:      {scan_result.get('language')}")
    print(f"  프레임워크: {scan_result.get('framework')}")
    print(f"  엔드포인트: {scan_result.get('endpoint_count')}개")

    files: list[dict] = scan_result.get("files", [])
    _print_section(f"스캔된 파일 ({len(files)}개)")
    for fi in files:
        eps = fi.get("endpoints", [])
        fns = fi.get("functions", [])
        models = fi.get("models", [])
        print(f"\n  [{fi['language']}] {fi['path']}")
        if eps:
            print(f"    엔드포인트 ({len(eps)}개):")
            for e in eps[:5]:
                print(f"      {e.get('method')} {e.get('path')}")
                print(f"        file={e.get('file')}  params={e.get('params')}  response_model={e.get('response_model')}")
            if len(eps) > 5:
                print(f"      ... 외 {len(eps) - 5}개")
        if fns:
            print(f"    함수 ({len(fns)}개):")
            for fn in fns[:5]:
                print(f"      {fn.get('name')}  params={fn.get('params')}  (type={type(fn.get('params')).__name__})")
            if len(fns) > 5:
                print(f"      ... 외 {len(fns) - 5}개")
        if models:
            print(f"    모델: {[m['name'] for m in models]}")
        deps = fi.get("dependencies", [])
        if deps:
            print(f"    의존성 ({len(deps)}): {deps[:5]}{'...' if len(deps) > 5 else ''}")

    git_diff = scan_result.get("git_diff")
    if git_diff:
        _print_section("Git Diff")
        print(f"  HEAD 커밋:    {git_diff.get('commit_hash', '')[:12]}")
        print(f"  이전 커밋:    {git_diff.get('prev_hash', '')[:12] or '(없음)'}")
        print(f"  변경 파일:    {len(git_diff.get('changed_files', []))}개")
        print(f"  추가 라인:    +{git_diff.get('added_lines', 0)}")
        print(f"  삭제 라인:    -{git_diff.get('deleted_lines', 0)}")
        print(f"  최신 커밋 작성자: {git_diff.get('author', '')} <{git_diff.get('author_email', '')}>")
        print(f"  커밋 시각:    {git_diff.get('commit_timestamp', '')}")
        changed = git_diff.get("changed_files", [])
        if changed:
            print(f"  변경 파일 목록: {changed[:10]}{'...' if len(changed) > 10 else ''}")


async def main() -> None:
    params = _collect_inputs()

    print(f"\n스캔 시작 → {params['repo_url']} ({params['branch']}) / trigger={params['trigger']}")

    tool = GitCodebaseScannerTool(trace_id="manual-test-001")
    tool_input = ToolInput(trace_id="manual-test-001", params=params)

    try:
        output = await tool.run(tool_input)
    except Exception as e:
        print(f"\n[오류] {type(e).__name__}: {e}")
        sys.exit(1)

    _print_section("Tool 실행 완료")
    print(f"  소요 시간: {output.metadata.get('duration_sec')}초")
    _print_scan_result(output.result)
    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    asyncio.run(main())
