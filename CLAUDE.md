# 프로젝트: 데이터 수집 및 분석 (가칭 : DataLens)
## 프로젝트 개요
다양한 원본 데이터를 하나의 분석 작업으로 관리하는 AI 데이터 품질 관리 플랫폼의 백엔드

## 기술 스택
Python + FastAPI(Modular Monolith) + MongoDB Atlas

## 명령어
가상환경은 Google Drive 동기화를 피하려고 프로젝트 밖에 둔다(예: `~/.venvs/datalens`).
- 설치: `py -3.14 -m venv ~/.venvs/datalens` → `~/.venvs/datalens/Scripts/python -m pip install -e ".[dev]"`
- 실행(API): `uvicorn app.main:create_app --factory --reload`
- 실행(워커): `python -m app.worker` (API와 별도 프로세스)
- 실행(DB 없이 로컬): `python scripts/dev_server.py` — 임시 MongoDB + API + 워커, 종료하면 데이터 삭제
- lint: `ruff check . && ruff format --check .`
- typecheck: `mypy app tests`
- test: `pytest` (DB 통합 테스트는 임시 MongoDB를 자동으로 띄움 — `MONGODB_TEST_URI`로 다른 DB 지정, `=skip`이면 건너뜀. 실제 LLM 호출 테스트는 `RUN_LLM_TESTS=1`일 때만)


## 구조
- `app/api/` — 요청 받고 응답 반환
- `app/services/` — 실제 업무 로직
- `app/processors/` — 파일 형식에 따른 추출 로직 분리
- `app/repositories/` — MongoDB 조회·저장
- `app/schemas/` — API 요청과 응답의 자료형 정의
- `app/core/` — 환경 변수와 보안 설정 관리
- `tests/` — 테스트

## 코드 규칙
- 에러는 `AppError` 클래스로 던지고 라우터에서 처리
- 새 기능에는 반드시 테스트 추가
- `any` 타입 사용 금지
- method 정의 시 반드시 주석으로 어떤 내용의 method이며, 파라미터는 어떤 것이 들어와야 하는 지 명시할 것
- 코드에 비밀 키를 넣지 않기

## 데이터베이스 설계 시 주의사항
- files에는 파일 전체 내용을 무조건 저장하지 않고, 원본 파일 저장 위치를 기록
- processing_jobs는 처리 실패 원인과 재처리 상태를 추적하는 데 사용
- processing_results는 정제 전후 결과를 구분해 버전별로 보관
- fileId, jobId, createdAt, status 등에 적절한 인덱스를 설정
- 대용량 정제 결과는 MongoDB 문서에 전부 넣기보다 별도의 산출물 파일로 저장하고, 결과 요약과 경로를 DB에 보관

## 파일 업로드 시 주의사항
- 확장자뿐 아니라 실제 파일 형식 검증

## 작업 흐름
- 커밋 메시지: `feat:`, `fix:`, `refactor:` 접두사 사용
- 작업 마치기 전 lint, typecheck, test 모두 통과 확인

## 주의
- DB 연결 문자열과 AI API 키는 .env에 보관
- 환경변수는 `.env.example` 참고, `.env, .env.example, .env.local` 등 관련 `.env` 파일들은 절대 커밋하지 않음

## 기준 문서 (충돌 시 위에서부터 우선)
1. DB: `docs/db-schema/schema.md` (원본 `docs/db-schema/*.xlsx`) — 필드명·타입·필수·설명을 그대로 따른다. 필드 추가·이름 변경 금지
2. API: `docs/api-spec/api-spec.md` (원본 `docs/api-spec/api_명세서.hwpx`) — 경로·기능·응답 예시를 그대로 따른다
3. 기능·설계: `docs/prd/PRD.md` — 7장(시스템 구조), 7.7(설계 파일 확인 사항), 12장(미결정 사항)을 먼저 확인
4. 화면 디자인: `docs/DESIGN.md`

- 기준에 없는 필드·컬렉션·API가 필요하면 만들지 말고 먼저 물어볼 것
- PRD에서 "제안", "명세서 외 추가 제안", 7.7·12장 항목은 확정 전이므로 구현 전에 확인할 것
- 원본(xlsx, hwpx)이 바뀌면 `.md` 변환본도 같이 갱신할 것
- `docs/downloadfile.pdf`(외부 API 15선 수집 가이드)는 기준 문서가 아님. 참고용으로만 보고, 내용(외부 API 수집, raw/clean 컬렉션 등)을 구현에 반영하지 않음

@docs/db-schema/schema.md
@docs/api-spec/api-spec.md
