"""blame_for_files — defect.file_location 직접 git blame 조회 (FAIL 선택 공유 담당자 추천).

git_diff.blame 은 최신 commit 의 changed_files 로만 한정되어, defect 의
file_path 가 그 이전에 마지막으로 바뀐 파일을 가리키면 _resolve_assignee 가
항상 None 을 반환한다 (run dea9fd4e 진단). blame_for_files 는 defect 가
가리키는 파일에 대해 직접 git blame 을 조회해 이를 보강한다.
"""
from __future__ import annotations

import git
import pytest

from qapilot.tools.codebase_scanner_tool import blame_for_files


@pytest.fixture
def repo_with_file(tmp_path):
    repo = git.Repo.init(tmp_path)
    actor = git.Actor("Alice", "alice@example.com")
    (tmp_path / "tier.py").write_text("def tier(): pass\n")
    repo.index.add(["tier.py"])
    repo.index.commit("add tier.py", author=actor, committer=actor)
    return tmp_path


class TestBlameForFiles:
    def test_exact_relative_path(self, repo_with_file):
        result = blame_for_files(repo_with_file, ["tier.py"])
        assert len(result) == 1
        assert result[0]["file"] == "tier.py"
        assert result[0]["author_email"] == "alice@example.com"

    def test_strips_repo_dir_name_prefix(self, repo_with_file):
        prefixed = f"{repo_with_file.name}/tier.py"
        result = blame_for_files(repo_with_file, [prefixed])
        assert len(result) == 1
        # "file" 키는 보정 전 원본 file_path 그대로 (defect_writer._resolve_assignee 매칭용)
        assert result[0]["file"] == prefixed
        assert result[0]["author_email"] == "alice@example.com"

    def test_nonexistent_file_returns_empty(self, repo_with_file):
        assert blame_for_files(repo_with_file, ["does_not_exist.py"]) == []

    def test_non_repo_path_returns_empty(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert blame_for_files(not_a_repo, ["tier.py"]) == []
