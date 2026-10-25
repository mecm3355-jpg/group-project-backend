# 프로젝트: 데이터 수집 및 분석 (가칭 : DataLens)
## 프로젝트 개요
다양한 원본 데이터를 하나의 분석 작업으로 관리하는 AI 데이터 품질 관리 플랫폼의 백엔드

## 기술 스택
Python + FastAPI(Modular Monolith) + MongoDB Atlas

## 명령어


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