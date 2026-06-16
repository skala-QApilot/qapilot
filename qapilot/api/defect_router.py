"""defect → GitHub issue 생성 라우터.

Author: C
Created: 2026-06-15
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from qapilot.api.agent_router import RepoPayload
from qapilot.api.internal_deps import verify_internal_token
from qapilot.api.response import fail, ok
from qapilot.db.defect_reader import load_defect, update_defect_issue_url
from qapilot.shared.errors import ErrorCode
from qapilot.shared.issue_template import load_template, render
from qapilot.shared.logger import get_logger
from qapilot.tools.git_codebase_scanner_tool import GitHubAdapter

router = APIRouter(
    prefix="/api/agent",
    tags=["defects"],
    dependencies=[Depends(verify_internal_token)],
)
logger = get_logger("api.defects")


class CreateGithubIssueRequestBody(BaseModel):
    service_id: str = Field(..., description="서비스 ID")
    repos: list[RepoPayload] = Field(..., description="GitHub 저장소 설정 목록")


def _select_repo(repos: list[RepoPayload], file_location: str | None) -> RepoPayload:
    """file_location 경로와 role 이 매칭되는 repo 를 우선 선택, 없으면 첫 repo."""
    if file_location:
        path = file_location.split(":")[0]
        for repo in repos:
            if repo.role and path.startswith(repo.role):
                return repo
    return repos[0]


@router.post("/defects/{defect_id}/github-issue")
async def create_github_issue(defect_id: str, body: CreateGithubIssueRequestBody) -> Any:
    """defect 의 원인 분석/해결 방안을 GitHub issue 로 생성한다."""
    defect = load_defect(defect_id)
    if defect is None:
        return fail(ErrorCode.AGENT_API_002, "defect를 찾을 수 없습니다.")
    if str(defect.get("service_id")) != body.service_id:
        return fail(ErrorCode.AGENT_API_003, "service_id가 defect와 일치하지 않습니다.")
    if not body.repos:
        return fail(ErrorCode.AGENT_API_003, "repos가 비어 있습니다.")

    repo = _select_repo(body.repos, defect.get("file_location"))
    if not repo.token:
        return fail(ErrorCode.AGENT_API_003, "선택된 repo에 token이 설정되어 있지 않습니다.")

    template = load_template(body.service_id)
    confidence = defect.get("root_cause_confidence")
    title, issue_body = render(template, {
        "tc_id": defect.get("tc_id"),
        "ts_id": defect.get("ts_id"),
        "category": defect.get("category"),
        "root_cause": defect.get("root_cause_top1"),
        "confidence": confidence,
        "solution_guide": defect.get("solution_guide"),
        "assignee": defect.get("assignee"),
        "file_location": defect.get("file_location"),
        "run_id": defect.get("run_id"),
    })

    adapter = GitHubAdapter(repo.repo_url, repo.token)
    issue = await adapter.create_issue(title, issue_body, labels=[defect["category"]])

    update_defect_issue_url(defect_id, issue["html_url"])

    logger.info(
        "github_issue_created",
        defect_id=defect_id, service_id=body.service_id,
        issue_number=issue["number"], issue_url=issue["html_url"],
    )

    return ok({
        "issue_url": issue["html_url"],
        "issue_number": issue["number"],
        "title": issue["title"],
    })
