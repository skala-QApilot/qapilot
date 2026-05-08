## 개요
QApilot CLI에서 생성된 로컬 산출물(.qapilot/scenarios, .qapilot/generated-code 등)을 중앙 서버로 전송하는 동기화 로직을 구현합니다.

## 관련 요구사항
- FR-016: CLI + 웹 대시보드
- FR-017: 캐시 모듈 (.qapilot/ 디렉토리 관리)

## 작업 내용
- [ ] `qapilot/cli/sync.py` 구현: 로컬 디렉토리 스캔 및 서버 업로드 로직
- [ ] `ApiClient` 확장: 파일 업로드 및 대량 전송 지원
- [ ] `qapilot sync` 명령어 추가 및 `generate` 완료 후 자동 동기화 옵션 구현

## 완료 조건 (Definition of DoD)
- [ ] 로컬의 시나리오 JSON 파일이 서버의 API를 통해 정상 전송됨
- [ ] `qapilot sync` 명령어 정상 동작
- [ ] 에러 핸들링 (네트워크 오류, 인증 오류 등) 반영

## 참고 사항
- 로컬과 서버의 데이터 정합성을 유지하기 위해 파일 해시 기반의 증분 전송 고려 (추후)

## 예상 소요 시간
1일