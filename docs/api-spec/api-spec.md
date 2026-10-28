# API 명세서 (기준)

> 원본: `docs/api-spec/api_명세서.hwpx`. 이 파일은 원본을 **그대로** 옮긴 Markdown 변환본이다. 원본이 바뀌면 이 파일도 함께 고친다.
> 경로·기능·예시를 임의로 바꾸지 않는다. 명세서에 없는 API(민원 전용 등)는 `docs/prd/PRD.md` 7.5의 "명세서 외 추가 제안"이며 확정 전이다.

## 파일 관리 API

| Method | Endpoint | 기능 |
| --- | --- | --- |
| POST | /api/v1/files | 파일 업로드 및 처리 작업 생성 |
| GET | /api/v1/files | 파일 목록 및 검색 |
| GET | /api/v1/files/{file_id} | 파일 상세 조회 |
| DELETE | /api/v1/files/{file_id} | 파일 및 관련 결과 삭제 |
| GET | /api/v1/files/{file_id}/preview | 원본 데이터 미리보기 |

※ 업로드 요청 : multipart/form-data

파일 등록 응답 예시

```json
{
"fileId": "68fb12a1...",
"originalName": "sales.csv",
"fileType": "csv",
"status": "queued",
"message": "파일이 등록되었습니다."
}
```

fileId는 UUID 또는 MongoDB의 _id를 문자열로 반환

## 처리 정제 API

| Method | EndPoint | 기능 |
| --- | --- | --- |
| POST | /api/v1/files/{file_id}/process | 데이터 추출·진단 작업 시작 |
| GET | /api/v1/jobs/{job_id} | 작업 상태 및 오류 확인 |
| GET | /api/v1/files/{file_id}/quality | 데이터 품질 진단 결과 |
| POST | /api/v1/files/{file_id}/cleaning-suggestions | AI 정제 규칙 추천 |
| POST | /api/v1/files/{file_id}/clean | 승인한 정제 규칙 적용 |
| GET | /api/v1/files/{file_id}/versions | 정제 버전 및 이력 조회 |

※ 정제 요청은 AI가 생성한 자유 형식의 코드를 받는 대신, 미리 정의된 규칙을 받도록 합니다.

처리 예시

```json
{
"rules": [
{
"type": "trim_whitespace",
"column": "customer_name"
},
{
"type": "remove_duplicates",
"columns": ["customer_id"]
},
{
"type": "fill_missing",
"column": "age",
"strategy": "median"
}
]
}
```

## 분석·AI·다운로드 API

| Method | EndPoint | 기능 |
| --- | --- | --- |
| POST | /api/v1/files/{file_id}/analysis | 분석 실행 |
| GET | /api/v1/files/{file_id}/analysis | 분석 결과 조회 |
| POST | /api/v1/files/{file_id}/report | AI 분석 리포트 생성 |
| GET | /api/v1/dashboard/summary | 전체 통계 조회 |
| GET | /api/v1/files/{file_id}/export?format=확장자 | 정제 결과 다운로드 |

※ 모든 API에서 성공·실패 응답 규격을 통일하세요. 작업이 오래 걸릴 수 있는 경우에는 요청을 받은 즉시 결과를 반환하기보다 작업 ID를 발급하고, 프론트엔드가 작업 상태를 조회하도록 구성하는 편이 안전
