# DB 스키마 (기준)

> 원본: `docs/db-schema/files.xlsx`, `processing_jobs.xlsx`, `processing_results.xlsx`. 이 파일은 원본을 **그대로** 옮긴 Markdown 변환본이다. 원본이 바뀌면 이 파일도 함께 고친다.
> 필드명·타입·필수·설명을 임의로 바꾸거나 필드를 추가하지 않는다. 원본 안에서 표와 예시 JSON이 다른 곳(예: processing_jobs 표의 `field`·`createAt` vs 예시의 `fileId`·`createdAt`)은 PRD 7.7의 확인 사항이며, 확정 전까지 **표를 기준**으로 한다.

## files

(파일 기본 정보 테이블)

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| _id | ObjectId | 자동 | 파일 고유 ID |
| originalName | String | O | 업로드 당시 파일명 |
| extension | String | O | 확장자. (ex : csv, pdf, jpg) |
| mimeType | String | O | 감지된 미디어 타입 |
| category | String | O | tabular, text, image, document, other |
| sizeBytes | Number | O | 파일 크기 (바이트) |
| storage | Object | O | 저장소 종류와 파일 위치 |
| status | String | O | 현재 처리 상태 |
| processingCapabilities | Object | O | 해당 파일에 지원되는 처리 기능 |
| createdAt | Date | O | 업로드 일시 |
| updatedAt | Date | O | 최종 수정 일시 |
| lastJobId | ObjectId / null | X | 마지막 처리 작업 ID |

예시 — 결과 파일 (.csv)

```json
{
  "_id": "ObjectId(...)",
  "originalName": "sales_2026.csv",
  "extension": "csv",
  "mimeType": "text/csv",
  "category": "tabular",
  "sizeBytes": 245760,
  "storage": {
    "provider": "local",
    "key": "uploads/2026/10/abc123"
  },
  "status": "completed",
  "processingCapabilities": {
    "preview": true,
    "qualityCheck": true,
    "cleaning": true,
    "statistics": true,
    "aiSummary": true
  },
  "createdAt": "2026-10-25T06:00:00.000Z",
  "updatedAt": "2026-10-25T06:05:00.000Z",
  "lastJobId": "ObjectId(...)"
}
```

예시 — 이미지 파일일 경우

```json
{
  "_id": "ObjectId(...)",
  "originalName": "store_photo.jpg",
  "extension": "jpg",
  "mimeType": "image/jpeg",
  "category": "image",
  "sizeBytes": 1850000,
  "storage": {
    "provider": "local",
    "key": "uploads/2026/10/def456"
  },
  "status": "completed",
  "processingCapabilities": {
    "preview": true,
    "qualityCheck": true,
    "cleaning": false,
    "statistics": false,
    "aiSummary": true
  },
  "createdAt": "2026-10-25T06:10:00.000Z",
  "updatedAt": "2026-10-25T06:12:00.000Z",
  "lastJobId": "ObjectId(...)"
}
```

## processing_jobs

(처리 작업 테이블)

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| _id | ObjectId | 자동 | 작업 ID |
| field | ObjectId | O | 대상 파일 ID |
| type | String | O | analyze, clean, report 등 |
| status | String | O | queued, processing, completed, failed |
| progress | Number | O | 진행률 ( 0 ~ 100 ) |
| stage | String | O | 현재 단계 |
| error | Object / null | - | 오류 코드와 메시지 |
| startedAt | Date / null | O | 시작 시간 |
| completedAt | Date / null | O | 완료 시간 |
| createAt | Date | O | 작업 생성 시간 |

비고: stage는 validating, parsing, quality_check, statistics, ai_summary처럼 세분화할 수 있습니다.

예시

```json
{
  "_id": "ObjectId(...)",
  "fileId": "ObjectId(...)",
  "type": "analyze",
  "status": "completed",
  "progress": 100,
  "stage": "completed",
  "error": null,
  "startedAt": "2026-10-25T06:01:00.000Z",
  "completedAt": "2026-10-25T06:05:00.000Z",
  "createdAt": "2026-10-25T06:01:00.000Z"
}
```

## processing_results

(분석 및 정제 결과 테이블)

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| _id | ObjectId | 자동 | 작업 ID |
| field | ObjectId | O | 대상 파일 ID |
| jobId | ObjectId | O | 생성한 작업 ID |
| resultType | String | O | analysis, cleaning, report |
| version | Number | O | 결과 버전 |
| quality | Object | O | 데이터 품질 진단 결과 |
| analysis | Object | O | 파일 유형별 분석 결과 |
| cleaning | Object | O | 적용한 정제 규칙 변경 내역 |
| ai | Object | O | AI 추천 및 요약 |
| createAt | Date | O | 결과 생성 시간 |

비고: 각 결과 객체는 해당 작업에 필요한 경우에만 저장

예시 — CSV, JSON 분석 결과

```json
{
  "quality": {
    "score": 92,
    "issues": [
      {
        "type": "missing_values",
        "severity": "medium",
        "count": 12,
        "location": "email",
        "message": "email 필드에 결측치가 있습니다."
      },
      {
        "type": "duplicates",
        "severity": "low",
        "count": 3,
        "location": null,
        "message": "중복 행 3개가 발견되었습니다."
      }
    ]
  },
  "analysis": {
    "format": "csv",
    "rowCount": 1000,
    "columnCount": 8,
    "columns": [
      {
        "name": "email",
        "inferredType": "string",
        "missingCount": 12,
        "uniqueCount": 950
      },
      {
        "name": "price",
        "inferredType": "number",
        "missingCount": 0,
        "uniqueCount": 120
      }
    ],
    "statistics": {
      "price": {
        "min": 1000,
        "max": 50000,
        "mean": 12500
      }
    }
  }
}
```

예시 — TXT, LOG 분석 결과

```json
{
  "quality": {
    "issues": [
      {
        "type": "empty_lines",
        "severity": "low",
        "count": 15,
        "location": null,
        "message": "빈 줄 15개가 발견되었습니다."
      }
    ]
  },
  "analysis": {
    "format": "log",
    "encoding": "utf-8",
    "lineCount": 5000,
    "emptyLineCount": 15,
    "keywordCounts": {
      "ERROR": 12,
      "WARN": 38,
      "INFO": 420
    }
  }
}
```

## 민원 전용 컬렉션(기준 아님)

categories·complaints·category_candidates·category_mappings·issues·reviews는 기준 파일에 없는 **PRD 제안**이다(`docs/prd/PRD.md` 5.2, 7.3, 7.4). 기준 파일과 같은 표 형식을 따르며, 확정 전까지 변경될 수 있다.
