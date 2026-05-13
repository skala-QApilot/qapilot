"""QApilot — AI 기반 테스트 시나리오/데이터 생성 및 통합 테스트 자동화 및 오류 분석 시스템."""

import warnings

# langgraph 가 모듈 로드 시점에 langchain-core 의 JsonPlusSerializer 를 인스턴스화하면서
# `allowed_objects` 디폴트 변경 예고로 발생시키는 노이즈 경고를 차단.
# langgraph / langchain-core 업데이트로 자연 해소될 때까지의 임시 suppression.
try:
    from langchain_core._api.deprecation import (
        LangChainPendingDeprecationWarning,
    )

    warnings.filterwarnings(
        "ignore", category=LangChainPendingDeprecationWarning
    )
except ImportError:
    pass
