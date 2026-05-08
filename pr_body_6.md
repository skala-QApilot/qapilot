## 관련 이슈
Closes #6

## 작업 내용
- QApilot CLI에서 생성된 로컬 산출물(.qapilot/)을 중앙 서버로 전송하는 동기화 로직 구현.
- `generate` 명령어 완료 후 자동 동기화 옵션 추가.

## 변경 사항
- [x] `qapilot/cli/sync.py` 구현: 로컬 디렉토리 스캔 및 서버 업로드 로직
- [x] `qapilot sync` 명령어 추가
- [x] `qapilot generate` 명령어에 `--sync/--no-sync` 옵션 추가 (기본값 sync)

## 테스트 확인
- [x] 로컬 실행 확인 (`qapilot sync` 호출 시 파일 탐색 로직 동작 확인)
- [x] 기존 기능 정상 동작 확인 (이전 CLI 기능 유지 확인)

## 리뷰어 참고 사항
- 현재는 시나리오(.json) 파일만 전송하도록 구현됨. 추후 생성된 코드(.js)도 묶어서 전송하는 로직으로 고도화 예정.
- `ApiClient`의 업로드 엔드포인트는 `implementation-plan.md` 사양을 준수함.