"""qapilot.scan — 메타데이터 추출 (PoC 2+, 데이터 layer).

frontend/backend/sut_tests 의 4 영역 메타데이터를 AST + LLM 으로 추출.
추출 결과는 qapilot.shared.metadata_schemas 의 Pydantic model 로 표현.

저장 (S3 writer) 은 PoC 3, 조회 는 PoC 4 에서 별도 모듈.
"""
