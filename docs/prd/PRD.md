> 원본: claude.ai 문서 'PRD v2' (Word 사본: 민원_자동분류_PRD_v2.docx). 이 파일은 Markdown 변환본이며, 그림 2개는 Word 파일에서 확인한다.

# 민원 원문 자동분류·분석 PRD v2

Oct 24, 2026 · @김경민 · 상태: 초안

## 1. 개요

분류 코드가 전혀 없는 민원 원문에서 **데이터가 스스로 카테고리를 만들고, 매달 그 체계를 키워 가는** 분석 파이프라인을 만든다. 첫 달에는 전체 민원을 군집화해 카테고리 v1을 만들고, 다음 달부터는 유사한 민원을 기존 카테고리에 넣고 새로운 유형만 신규 카테고리로 추가한다. 카테고리는 사람이 승인해야 확정되고, 모든 통계는 어느 체계 버전으로 집계했는지 함께 남긴다.

### 문제 정의

- 국민신문고, 전화 상담 메모, 게시판 민원 등 원문만 있는 채널은 내용 분류가 없다.
- 분류가 있는 채널도 기관마다 체계가 다르고(부서 기준, 숫자 코드, 처리 구분), 너무 굵어 정책 판단에 쓰기 어렵다.
- 같은 장소·같은 문제의 반복 민원이 개별 건으로 흩어져 근본 원인이 보이지 않는다.

### 목표

- 미리 정한 분류 없이, 민원 내용의 유사성만으로 카테고리 후보를 찾아내고 이름을 제안한다.
- 담당자가 후보를 승인·병합·분할해 카테고리 체계를 확정하고, 버전으로 관리한다.
- 매달 새 민원을 기존 카테고리에 자동 배정하고, 배정되지 않는 유형은 신규 카테고리로 제안한다.
- 민원마다 핵심 속성(대상 시설, 장소, 긴급도, 반복 여부 등)을 추출한다.
- 유사·반복 민원을 묶어 "이슈" 단위로 집계하고, 월별 통계·급증·신규 유형을 대시보드로 보여준다.

### 비목표

- 민원 답변 작성, 담당 부서 자동 배정, 처리 자동화
- 실시간 접수 시스템 연동(초기 단계는 파일 일괄 처리)
- 자동분류 결과를 사람 검토 없이 공식 통계로 확정하는 것

### v1 대비 변경점

| 항목 | v1 | v2 |
| --- | --- | --- |
| 입력 | 기관별 CSV(기존 분류 포함) | 분류 없는 원문 중심 |
| 카테고리 생성 | 사람이 미리 정의(하향식) | 데이터에서 발견 후 승인(상향식) |
| 체계 변화 | 고정 | 매달 신규 카테고리 추가, 분기마다 정리 |
| 분류 결과 | 대표 카테고리 1개 | 카테고리 + 상위 그룹 + 속성 6종 |
| 집계 단위 | 민원 건 | 민원 건 + 이슈(반복 묶음) |
| 출력 | CSV | API(FastAPI) + MongoDB Atlas + 대시보드 |

## 2. 사용자와 활용 시나리오

주 사용자는 민원 데이터를 정리·보고하는 분석 담당자다. 보조 사용자는 결과를 읽고 판단하는 부서 담당자·관리자다.

| 사용자 | 하는 일 | 필요한 결과 |
| --- | --- | --- |
| 분석 담당자 | 원문 파일을 올리고 분류·집계를 실행, 저신뢰 건 검토 | 분류 결과 CSV, 검토 대기 목록, 실행 로그 |
| 부서 담당자 | 자기 업무 유형의 반복 장소·긴급 건 확인 | 유형별 상세, 반복 이슈 목록 |
| 관리자 | 월간 민원 동향 보고 | 요약 대시보드, 급증·신규 이슈 |

### 대표 시나리오

1. **첫 달 카테고리 만들기**: 분석 담당자가 한 달치 원문을 넣으면 비슷한 민원끼리 묶인 후보 목록이 나온다. 후보마다 제안 이름, 핵심어, 대표 민원 5건이 붙고, 담당자는 승인·이름 수정·합치기·나누기를 해 카테고리 v1을 확정한다.
2. **다음 달 갱신**: 새 민원을 넣으면 대부분은 기존 카테고리에 자동 배정되고, 어디에도 맞지 않는 민원만 모여 신규 카테고리 후보로 제안된다. 담당자가 승인하면 v2가 된다.
3. **월간 동향 보고**: 카테고리별 건수와 전월 대비 증감, 급증 카테고리, 이번 달 신규 카테고리가 대시보드로 나온다.
4. **반복 민원 추적**: 부서 담당자가 카테고리를 열면, 같은 장소에서 3회 이상 접수된 이슈가 건수 순으로 보인다.
5. **분기 정리**: 담당자가 겹치는 카테고리를 합치거나 너무 커진 카테고리를 나눈다. 변경 내역은 매핑표로 남아 과거 통계도 새 체계로 다시 집계할 수 있다.

## 3. 입력 데이터와 샘플셋

분석 샘플은 첨부 샘플 묶음(파일 15개, 6종)으로 확정한다. 형식(CSV·XLSX·스캔 PDF)과 성격이 서로 달라 파이프라인의 다른 부분을 검증한다. 자유 서술 원문은 가상민원 1,000건과 포항 113건뿐이고, 아산·김천은 원문 없이 유형·처리 정보만 있는 대용량 실제 데이터다.

| 샘플 | 형식 | 건수·기간 | 내용 | 정답·기존 분류 | 검증 용도 |
| --- | --- | --- | --- | --- | --- |
| 생활불편\_가상민원\_1000건\_날짜재배정.xlsx | XLSX | 1,000건, 2026-01\~10 | 합성 원문 | 대분류 25종, 세부분류, 긴급도 | 발견·배정·신규 카테고리·급증 월별 시뮬레이션, 속성 추출 정확도 |
| 포항시\_불편신고접수\_20230828.csv | CSV(CP949) | 116행(본문 113), 2018-10\~2023-05 | 실제 시민 원문, 좌표·주소 | 숫자 분류번호(코드표 없음) | 실제 원문 적용, 수작업 평가셋, 지도·이슈 묶기, 정제 규칙 |
| 국민신문고1\~10.pdf | PDF(스캔, 11쪽×10개) | 500건, 2026-10-20\~24 | 제목·처리기관·등록일·조회수 | 없음 | OCR 추출과 문서형 민원 분리(7.6) |
| 국민신문고\_OCR\_민원제목.csv | CSV | 500건 | 위 PDF를 OCR한 뒤 사람이 교정한 결과 | 위 PDF 추출의 정답 | OCR 정확도 측정 기준, 짧은 제목 분류·묶기 |
| 아산시\_생활민원처리현황\_20250901.csv | CSV | 27,909건, 2024-04\~2025-08(17개월) | 원문 없음. 민원유형-중 20종, 처리부서 81·팀 235, 접수일시 | 기관 분류(민원유형-중) | 실제 시계열로 월별 통계·급증 탐지·대시보드 검증, 기관 분류와 상위 그룹 매핑, 유형별 담당 부서 분석 |
| 김천시\_안전신문고 불법주정차\_20251118.csv | CSV | 8,811건, 2024-01\~12 | 정형 제목(위반 장소 + 일부 자유 서술), 처리 결과 | 처리결과(민원요지), 민원유형(일반·취하·다부처) | 정형 문구에서 세부유형 규칙 추출, 처리결과 값 정규화, 단일 유형의 월별 추이·급증 |

### 샘플별 주의점

- **가상민원**: 템플릿 문장이라 성능이 실제보다 높게 나올 수 있고, 라벨과 본문이 어긋난 사례가 있다(예: SYN-0001 "점자블록이 깨져" → 세부분류 "도로 침하"). 접수일시는 계절성·첫 등장·급증을 넣어 재배정했다(자전거 4월, 하천 5월, 여름철 6월, 불법광고물 8월 첫 등장. 배수·침수 7월, 소음·진동 9월 급증). 원래 날짜는 별도 열, 생성 규칙은 별도 시트에 있다.
- **포항**: 31행은 번호·날짜·위치가 없고, 그중 7행은 시스템·테스트 문구, 4행은 한 민원이 나뉜 것으로 보인다. 경도·위도 열 이름이 서로 바뀌어 있고, 좌표 1건은 대구다.
- **국민신문고**: 법령 해석 질의·정책 건의 비중이 높고(국토교통부 96건) 생활불편과 결이 다르다. 실명이 든 제목이 2건 있다. OCR만으로는 약 140건(28%)을 고쳐야 했다.
- **아산**: 자유 서술 원문이 없어 내용 기반 분류·발견의 대상은 아니다. 대신 실제 계절 패턴이 뚜렷해(2024-07 2,293건·2025-07 2,516건 정점, 2025-01 926건 최저) 급증 규칙과 월별 통계를 검증하기에 가장 좋다. 2024-04와 2025-08은 일부 월이다. 중복 3행, 날짜 오류 1행이 있고, 유형 코드 번호가 겹친다(01.교통관리·01.건축건설).
- **김천**: 제목이 약 60자에서 잘려 있고, "(SPP-번호) ○○ 불법 주정차 신고입니다." 뒤에 자유 서술이 조금 붙어 있다. 위반 장소는 횡단보도 3,092·인도 1,873·소화전 1,192·교차로 모퉁이 1,122건 순이다. 처리결과에 오타 변형이 있고(과태교·과태룹·과태료미부과 등), 10월에 1,135건으로 급증한다.

### 입력 형식

- 원본 데이터는 형식이 제각각이다. 지원 형식은 CSV·TSV, XLSX·XLS, DOCX, PDF(텍스트형), PDF(스캔형)·이미지(JPG·PNG), TXT이고, HWP·HWPX는 후보다(공공기관 문서에 많음).
- 모든 파일은 API(`POST /api/v1/files`)로 업로드하고, 확장자뿐 아니라 파일 서명(매직 바이트)과 내부 구조로 실제 형식을 검증한다.
- \*\*표 형태(CSV·XLSX)\*\*는 1행 = 민원 1건이다. 소스별 컬럼 이름은 업로드 때 매핑 정보로 등록해 공통 필드에 맞춘다.
- \*\*문서 형태(DOCX·PDF·이미지·TXT)\*\*는 문서 안에서 민원 단위를 나눠야 한다. 표의 행, 반복되는 서식(예: 국민신문고 목록의 번호·제목·처리기관 블록), 또는 파일 1개 = 1건 중 하나로 나눈다(7.6).
- 필수 값은 민원 내용 1개. 선택 값은 접수일시, 지역, 좌표, 처리기관, 원본 ID다.
- 모든 민원에 원문 위치(CSV·XLSX는 행 번호, PDF는 페이지·블록, DOCX는 문단·표 행)를 남겨 원본으로 되돌아갈 수 있게 한다.
- 원본 파일은 저장소에 두고 DB(`files`)에는 위치·해시·크기만 남긴다.

샘플에서 제외한 파일: 아산 생활민원처리시스템 민원정보(50MB, 원문 없는 처리·이관 이력 — 처리 지연 분석 후보), 민원\_통합정리.csv(원천 재가공본), 권익위 결정례집 PDF, 포트홀 API 안내 DOCX, 날짜 재배정 전 가상민원 원본. 민원\_샘플\_통합.csv는 샘플 일부를 손으로 합친 참고용이며, 구현 후에는 파이프라인의 정규화 결과로 대체한다.

## 4. 처리 파이프라인

매달 원문은 두 갈래로 나뉜다. 기존 카테고리와 충분히 비슷한 민원은 바로 배정돼 통계에 들어가고, 나머지는 미배정 풀에 모여 신규 카테고리 후보가 된다. 담당자가 승인한 후보는 다음 달부터 배정 대상이 된다. 첫 달에는 카테고리가 없으므로 전체가 미배정 풀로 간다.

> [그림: 월별 처리 흐름 · 배정 또는 신규 후보, 승인 후 체계 갱신 — 원본 PRD 문서/Word 파일 참고]

1. **입력·정규화**: 파일 형식 검증, 형식별 추출과 민원 단위 분리(7.6), 소스별 컬럼 매핑, 인코딩 감지, 공백·줄바꿈·날짜 정리. 빈 본문과 완전 중복은 오류 리포트로 빼고 나머지는 계속 처리한다.
2. **개인정보 마스킹**: 10장 기준으로 치환. 이후 단계는 마스킹본만 본다.
3. **임베딩·속성 추출**: 민원마다 문장 임베딩을 만들고 속성 6종(5.5)을 뽑는다.
4. **카테고리 배정**: 현재 체계의 카테고리 중심들과 유사도를 비교한다. 기준 이상이면 배정, 기준 미만이면 미배정, 1·2위 차이가 작으면 애매(검토)로 표시한다(5.3).
5. **미배정 풀 → 군집화·이름 제안**: 미배정 민원을 군집화하고, 최소 규모를 넘는 군집마다 핵심어·대표 민원·제안 이름을 붙여 후보로 올린다. 이번 달 후보가 아닌 민원은 풀에 남아 다음 달에 다시 본다.
6. **담당자 승인**: 후보를 승인·보류하거나 기존 카테고리에 합친다. 승인되면 체계 버전이 올라가고, 그 카테고리에 속한 이번 달 민원도 소급해 배정한다.
7. **이슈 묶기**: 같은 카테고리 + 같은 장소(좌표 100m 이내 또는 같은 장소 표현) + 본문 유사도 0.8 이상이면 한 이슈로 묶는다.
8. **집계·대시보드**: 8장의 지표를 계산해 API(/stats)로 제공하고, 전체 결과는 산출물 파일로 내보낸다. 각 단계의 진행·실패는 작업(job)으로 추적한다(7.2).

유사도·거리·최소 규모 수치는 모두 초기값이며, M2 시뮬레이션 결과로 조정한다. 분기마다 하는 병합·분할 정리는 5.3에 있다.

## 5. 카테고리 생성·확장 체계

카테고리는 미리 정하지 않고 데이터에서 찾는다. 첫 달에 카테고리 v1을 만들고, 이후에는 매달 "기존 카테고리에 배정 → 남은 민원에서 신규 카테고리 발견 → 승인" 순서로 체계를 키운다. 체계는 MongoDB의 `categories` 컬렉션에서 관리하고, 승인할 때마다 버전이 올라간다.

### 5.1 월별 운영 절차

| 단계 | 1개월차 | 2개월차 이후 |
| --- | --- | --- |
| 배정 | 없음(체계가 없음) | 새 민원을 기존 카테고리 중심과 비교해, 유사도가 기준값 이상이면 배정 |
| 발견 | 전체 민원 군집화 | 배정되지 않은 민원(미배정 풀)만 군집화 |
| 명명 | 군집별 핵심어·대표 민원으로 이름 제안 | 동일 |
| 승인 | 담당자가 승인·수정·합치기·나누기 → v1 | 신규 후보만 승인 → v2, v3 … |
| 집계 | v1 기준 통계 | 해당 월 버전 기준 통계 + 전월 비교 |

승인되지 않은 후보에 속한 민원은 "미배정"으로 남고 다음 달 발견에 다시 들어간다.

### 5.2 카테고리 레코드(categories 스키마)

카테고리는 `categories` 컬렉션에 체계 버전별로 저장한다. 표기는 DB 스키마 설계 파일과 같다(필드는 camelCase, 필수는 자동 / O / X).

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 문서 ID |
| categoryId | String | O | 카테고리 고유 ID(예: CAT-0012). 이름이 바뀌어도 유지 |
| version | Number | O | 이 레코드가 속한 체계 버전 |
| name | String | O | 승인된 이름(예: 가로등 고장) |
| group | String | O | 상위 그룹(예: 안전·조명). 승인 때 함께 지정 |
| definition | String | O | 한 줄 정의와 경계 메모 |
| keywords | Array | O | 핵심어(최대 10개) |
| exemplarIds | Array | O | 대표 민원 5건의 complaints.\_id |
| centroid | Array | O | 승인 시점 민원들의 임베딩 평균(Number 배열). 이후 배정된 민원으로 바꾸지 않음 |
| embeddingModel | String | O | 중심 벡터를 만든 임베딩 모델 |
| status | String | O | active, merged, split |
| createdInVersion | Number | O | 처음 추가된 체계 버전 |
| approvedBy | String | O | 승인자 |
| approvedAt | Date | O | 승인 일시 |
| createdAt | Date | O | 생성 일시 |
| updatedAt | Date | O | 최종 수정 일시 |

인덱스: categoryId + version(고유), status, group

```json
{
  "_id": "ObjectId(...)",
  "categoryId": "CAT-0012",
  "version": 3,
  "name": "가로등 고장",
  "group": "안전·조명",
  "definition": "가로등·보안등이 꺼지거나 깜빡임. 설치 요청은 제외",
  "keywords": ["가로등", "깜빡", "꺼짐", "보안등", "어두움"],
  "exemplarIds": ["ObjectId(...)", "ObjectId(...)"],
  "centroid": [0.0132, -0.0418, 0.0275],
  "embeddingModel": "bge-m3",
  "status": "active",
  "createdInVersion": 1,
  "approvedBy": "analyst01",
  "approvedAt": "2026-11-03T01:20:00.000Z",
  "createdAt": "2026-11-03T01:20:00.000Z",
  "updatedAt": "2026-11-03T01:20:00.000Z"
}
```

### 5.3 운영 규칙

| 규칙 | 초기값 | 이유 |
| --- | --- | --- |
| 배정 기준 유사도 | 코사인 0.75 (가상민원으로 조정) | 너무 낮으면 다른 민원이 기존 카테고리로 흡수되고, 너무 높으면 신규가 남발됨 |
| 애매 구간 | 1위와 2위 카테고리 유사도 차이 0.05 미만 | 검토 대기로 보냄 |
| 신규 카테고리 최소 규모 | 같은 군집 5건 이상, 또는 2개월 연속 등장 | 한두 건짜리 특이 민원으로 카테고리가 늘어나는 것을 막음 |
| 중심 고정 | 승인 시점 centroid 유지 | 매달 비슷한 건이 붙으면서 카테고리 의미가 넓어지는 것을 막음 |
| 분기 정리 | 3개월마다 | 중심끼리 유사도 0.85 이상인 쌍은 병합 후보, 내부 편차가 큰 카테고리는 분할 후보로 제시 |
| 버전 매핑 | 병합·분할 시 구 → 신 매핑표 자동 기록 | 과거 통계를 새 체계로 다시 집계 |

기준값은 모두 초기값이다. 첫 달이 특정 계절(예: 10월)이면 제설·해충 같은 계절 유형이 없지만, 해당 계절에 신규로 추가되므로 체계는 약 1년에 걸쳐 안정된다.

### 5.4 초기 상위 그룹(참고용)

카테고리는 데이터에서 나오지만, 보고서가 길어지지 않도록 상위 그룹은 대략 정해 둔다: 교통·주정차, 대중교통, 도로·보행, 안전·조명, 환경·청소, 물·하천, 공원·공공시설, 소음·건축·주거, 행정·법령 질의. 발견된 카테고리가 어느 그룹에도 안 맞으면 그룹도 새로 만든다. 분류는 카테고리 단위로 하고, 그룹은 집계에만 쓴다.

기관이 이미 붙여 온 분류(아산 민원유형-중 20종, 김천 위반 장소 유형·처리결과, 포항 분류번호)는 `sourceCategory`로 그대로 보존하고, 상위 그룹과의 매핑표를 따로 만들어 지역 간 비교에 쓴다. 예: 아산 05.도로관리 → 도로·보행, 김천 전체 → 교통·주정차. 이 값들은 발견된 카테고리의 정답으로 쓰지 않는다.

### 5.5 속성 6종

| 속성 | 값 | 추출 기준 |
| --- | --- | --- |
| 대상 시설 | 자유 텍스트 → 시설 사전으로 정규화 | 문제가 발생한 물건·시설(가로등, 보도블록, 정류장 등) |
| 장소 | 행정동 + 장소 표현 | 본문에 적힌 동·읍·면과 지물(예: 수청동 / 놀이터 옆). 좌표가 있으면 좌표 우선 |
| 긴급도 | 긴급 / 높음 / 보통 / 낮음 | 인명·사고 위험 언급 = 긴급, 생활 지장 = 높음, 불편 = 보통, 건의·문의 = 낮음 |
| 반복 여부 | 최초 / 재신고 | "다시", "여러 번", "지난달에도" 등 본문 표현 |
| 요청 유형 | 수리·조치 / 단속 / 신설·개선 / 질의 / 불만 | 민원인이 바라는 행동 |
| 영향 대상 | 어린이 / 노인 / 장애인 / 일반 | 본문에 언급된 취약계층 |

본문에 근거가 없는 속성은 빈 값으로 둔다. 추측으로 채우지 않는다.

## 6. 분석기 방식

분석은 네 가지 일로 나뉜다: 임베딩, 발견(군집화), 명명, 배정·속성 추출. 임베딩과 군집화는 항상 로컬에서 돌리고, 명명과 속성 추출에만 LLM을 선택적으로 쓴다. LLM을 쓸 때는 마스킹본만 보낸다. 어느 쪽을 기본으로 운영할지는 M2 비교 결과로 정한다.

| 일 | A. 로컬 방식 | B. LLM 선택 옵션(Azure OpenAI gpt-5-mini) |
| --- | --- | --- |
| 임베딩 | 한국어 문장 임베딩(bge-m3 등), 항상 로컬 | 해당 없음 |
| 발견(군집화) | UMAP 차원 축소 + HDBSCAN. 군집 수를 미리 정하지 않고 잡음은 미배정으로 남김 | 해당 없음 |
| 명명 | c-TF-IDF 핵심어(BERTopic 방식) + 대표 민원 → 담당자가 이름 작성 | 핵심어·대표 민원을 주고 이름·정의·상위 그룹 초안 생성 |
| 배정 | 카테고리 중심과의 코사인 유사도 | 애매 구간만 카테고리 정의를 주고 재판단 |
| 속성 추출 | 규칙·사전(Kiwi 형태소 분석 + 행정동·시설·긴급 표현 사전) | JSON 스키마로 구조화 출력 |
| 장점 | 원문이 밖으로 나가지 않음, 비용 0, 재현 가능 | 이름 초안과 속성 품질이 높음, 담당자 작업 감소 |
| 단점 | 이름을 사람이 직접 짓고 속성은 규칙 수준 | 마스킹본이라도 외부 전송, 건당 비용 |

### 공통 인터페이스

```
discover(records) -> list[CandidateCategory]
assign(records, taxonomy) -> list[Assignment]
extract(records) -> list[Attributes]
```

- 입력은 마스킹된 본문이다. discover는 카테고리 후보, assign은 배정 결과와 유사도, extract는 속성 6종을 돌려준다.
- 분석기 이름·버전·사용 모델을 결과 행마다 기록한다.
- LLM 방식은 temperature 0, 프롬프트 버전 고정, 응답 캐시로 재현성을 확보한다.
- 임베딩 모델을 바꾸면 모든 카테고리 중심을 다시 계산해야 하므로, 모델 교체는 분기 정리 때만 한다.

구현 위치: discover·assign·extract는 `app/services/`에 두고, 임베딩 모델과 LLM 호출은 교체 가능한 어댑터로 분리한다. 모든 메서드에는 기능과 파라미터를 설명하는 주석을 달고, 타입은 `typing.Any` 없이 스키마(`app/schemas/`)로 명시한다. LLM API 키는 `.env`에서만 읽는다.

### 6.1 LLM 배포(Azure OpenAI)

LLM은 이미 배포된 Azure OpenAI(Azure AI Foundry)를 쓴다. 임베딩·군집화·배정은 로컬에서 하고, LLM은 이름 짓기와 속성 추출에만 쓴다.

| 배포 이름 | 모델 | 역할 |
| --- | --- | --- |
| gpt-5-mini-1 | gpt-5-mini | 기본 LLM. 속성 추출, 카테고리 이름·정의·상위 그룹 초안, 애매 배정 재판단 |
| gpt-4.1-mini-1 | gpt-4.1-mini | M2 비교용 예비 |

| 용도 | 호출량 | 적용 |
| --- | --- | --- |
| 카테고리 이름·정의 초안 | 매달 신규 후보 수만큼(수십 회) | M3부터 사용 |
| 속성 추출 | 민원 20\~50건을 한 번에 묶어 호출 | M2에서 규칙 방식과 비교해 결정 |
| 애매 배정 재판단 | 1·2위 유사도 차이가 작은 건만 | 선택 |
| 임베딩·군집화 | — | 쓰지 않음(로컬) |

구현 규칙:

- 연결 정보는 `.env`에만 둔다: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`(= gpt-5-mini-1, 모델명이 아닌 배포 이름), `AZURE_OPENAI_API_VERSION`(Foundry 배포 상세의 예제 코드 값). `.env.example`에는 변수 이름만 둔다.
- 호출은 `app/services/`의 LLM 어댑터 하나로만 하고, `openai` 패키지의 `AzureOpenAI` 클라이언트를 쓴다.
- 마스킹본만 보낸다. 결과는 JSON 스키마(구조화 출력)로 받아 `app/schemas/`로 검증하고, 형식이 깨진 응답은 행 단위 오류로 기록한다.
- GPT-5 계열은 무작위성 설정(temperature)을 고정할 수 없을 수 있다. 재현성은 프롬프트 버전 + 입력 해시 기준 응답 캐시로 확보한다.
- 호출 건수·토큰 수·실패를 `processing_results.ai`에 기록한다.

## 7. 시스템 구조와 데이터 저장

민원 분석은 DataLens 백엔드(Python + FastAPI 모듈형 모놀리스 + MongoDB Atlas)의 한 분석 모듈로 구현한다. 원본 파일은 저장 위치만 기록하고, 처리는 작업(job) 단위로 추적하며, 대용량 결과는 별도 산출물 파일로 두고 DB에는 요약과 경로만 남긴다.

### 7.1 모듈 배치

| 위치 | 역할 | 민원 분석에서 맡는 것 |
| --- | --- | --- |
| `app/api/` | 요청을 받고 응답 반환, `AppError` 처리 | 파일 업로드, 작업 생성·조회, 카테고리 승인, 통계 조회 라우터 |
| `app/services/` | 업무 로직 | 정규화, 마스킹, 임베딩·속성 추출, 배정, 발견(군집화·명명), 이슈 묶기, 집계 |
| `app/processors/` | 파일 형식별 추출 | CSV, XLSX, DOCX, PDF(텍스트·스캔 OCR), 이미지, TXT 추출기와 문서형 파일의 민원 분리기(7.6) |
| `app/repositories/` | MongoDB 조회·저장 | 7.3의 컬렉션별 저장소 |
| `app/schemas/` | 요청·응답 자료형 | 작업, 카테고리, 후보, 민원 분석 결과, 통계 응답 |
| `app/core/` | 환경 변수·보안 설정 | DB 연결 문자열, AI API 키(.env), 기준값 설정 |
| `tests/` | 테스트 | 추출기·마스킹·배정 단위 테스트, 가상민원 월별 시뮬레이션 통합 테스트 |

### 7.2 작업 흐름

1. `POST /api/v1/files`(multipart/form-data): 확장자와 실제 형식을 모두 검증하고, 원본은 저장소에 둔 뒤 `files`에 등록한다. 명세서대로 업로드와 함께 기본 처리 작업(추출·진단)이 만들어진다.
2. `GET /api/v1/files/{file_id}/preview`: 추출 미리보기(앞 20건)로 컬럼 매핑과 민원 분리 규칙을 확정한다(7.6).
3. `POST /api/v1/files/{file_id}/analysis`(type = complaint): 민원 분석 작업(`processing_jobs.type` = complaint\_analysis)을 만든다. 상태는 queued → processing → completed / failed.
4. 작업은 `stage`와 `progress`로 진행 상황을 남긴다. 실패하면 그 시점의 stage가 남고 `error`에 오류 코드와 메시지를 기록한다. 재처리는 같은 파일로 새 작업을 만든다.
5. 결과는 `processing_results`(quality: 행 단위 오류·품질 문제, analysis: 추출 정보·민원 요약·산출물 경로, ai: LLM 사용 내역), `complaints`(민원 단위), 산출물 파일(전체 결과 CSV·임베딩)로 나눠 저장한다(7.3).

### 7.3 컬렉션

기준은 `데이터베이스_스키마_설계/` 폴더의 3개 파일(files.xlsx, processing\_jobs.xlsx, processing\_results.xlsx)이다. 아래 세 표는 설계 파일의 필드·타입·필수·설명을 **원문 그대로** 옮긴 것이며, 필드를 더하거나 고치지 않는다. 민원 분석에 필요한 정보는 기준 필드 안에 담는다(7.3 끝의 사용 방식). 민원 전용 컬렉션도 같은 표 형식(필드 | MongoDB 타입 | 필수 | 설명, 필수는 자동 / O / X)을 따른다.

**files** — 파일 기본 정보 테이블(files.xlsx)

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 파일 고유 ID |
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

예시(설계 파일 원문) — CSV 파일, 이미지 파일

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

**processing\_jobs** — 처리 작업 테이블(processing\_jobs.xlsx)

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 작업 ID |
| field | ObjectId | O | 대상 파일 ID |
| type | String | O | analyze, clean, report 등 |
| status | String | O | queued, processing, completed, failed |
| progress | Number | O | 진행률 ( 0 \~ 100 ) |
| stage | String | O | 현재 단계 |
| error | Object / null | - | 오류 코드와 메시지 |
| startedAt | Date / null | O | 시작 시간 |
| completedAt | Date / null | O | 완료 시간 |
| createAt | Date | O | 작업 생성 시간 |

비고(원문): stage는 validating, parsing, quality\_check, statistics, ai\_summary처럼 세분화할 수 있습니다.

예시(설계 파일 원문)

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

**processing\_results** — 분석 및 정제 결과 테이블(processing\_results.xlsx)

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 작업 ID |
| field | ObjectId | O | 대상 파일 ID |
| jobId | ObjectId | O | 생성한 작업 ID |
| resultType | String | O | analysis, cleaning, report |
| version | Number | O | 결과 버전 |
| quality | Object | O | 데이터 품질 진단 결과 |
| analysis | Object | O | 파일 유형별 분석 결과 |
| cleaning | Object | O | 적용한 정제 규칙 변경 내역 |
| ai | Object | O | AI 추천 및 요약 |
| createAt | Date | O | 결과 생성 시간 |

비고(원문): 각 결과 객체는 해당 작업에 필요한 경우에만 저장

예시(설계 파일 원문) — CSV·JSON 분석 결과, TXT·LOG 분석 결과

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

**민원 분석에서 기준 필드를 쓰는 방식**

새 필드를 만들지 않고, 기준 필드의 값과 Object 안에 담는다.

| 필요한 정보 | 담는 위치 |
| --- | --- |
| 파일 형식 분류 | files.category: CSV·XLSX → tabular, TXT → text, JPG·PNG → image, PDF·DOCX·HWPX → document |
| 민원 분석 작업 | processing\_jobs.type = "complaint\_analysis"(설명의 "analyze, clean, report 등"에 더하는 값) |
| 민원 분석 단계 | processing\_jobs.stage: validating → parsing → masking → embedding → assigning → discovering → issue\_grouping → statistics → ai\_summary(비고의 "세분화") |
| 실패 단계·원인 | 실패 시점의 processing\_jobs.stage + error {code, message} |
| 재처리 | 같은 파일로 새 작업을 만든다. 이력은 field(파일 ID)로 작업 목록을 조회해 확인 |
| 추출 정보(형식·인코딩·행/페이지 수·OCR 여부) | processing\_results.analysis {format, encoding, rowCount, pageCount, method} — 기준 예시의 analysis 형식 |
| 민원 분석 요약 | processing\_results.analysis.complaint {total, assignedRate, unassignedRate, reviewCount, candidateCount, taxonomyVersion, period} |
| 대용량 결과 파일 경로 | processing\_results.analysis.artifacts \[{kind, storageKey}\] |
| 행 단위 오류·데이터 품질 문제 | processing\_results.quality.issues — 기준 형식 {type, severity, count, location, message} |
| LLM 호출 건수·토큰·실패, 이름 제안 | processing\_results.ai |
| 처리 월·체계 버전 지정 | 작업에 담을 필드가 없다. 처리 월은 민원 접수일로 계산하고 체계 버전은 실행 시점의 최신 버전을 쓴다 |

샘플에서 나온 품질 문제를 기준 형식으로 적으면 다음과 같다(포항 불편신고 파일).

```json
{
  "quality": {
    "issues": [
      { "type": "missing_metadata", "severity": "medium", "count": 31, "location": null, "message": "번호·날짜·위치가 없는 행 31개가 발견되었습니다." },
      { "type": "system_text", "severity": "low", "count": 7, "location": "신고상담내용", "message": "시스템·테스트 문구로 보이는 행 7개가 발견되었습니다." },
      { "type": "split_rows", "severity": "medium", "count": 4, "location": "신고상담내용", "message": "한 민원이 여러 행으로 나뉜 것으로 보이는 행 4개가 발견되었습니다." },
      { "type": "column_swap", "severity": "high", "count": 72, "location": "경도, 위도", "message": "경도·위도 열 이름이 서로 바뀌어 있습니다." },
      { "type": "out_of_range", "severity": "low", "count": 1, "location": "경도, 위도", "message": "포항 범위 밖 좌표 1개가 발견되었습니다." },
      { "type": "missing_values", "severity": "low", "count": 3, "location": "신고상담내용", "message": "신고상담내용 필드에 결측치가 있습니다." }
    ]
  }
}
```

인덱스(제안, 기준 필드만 사용): files — createdAt, status, lastJobId / processing\_jobs — field, status, type, createAt / processing\_results — jobId, field, resultType + version

**민원 전용 컬렉션** — complaints는 7.4, categories는 5.2에 있다.

**category\_candidates** — 신규 카테고리 후보와 승인 결정

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 후보 ID |
| jobId | ObjectId | O | 후보를 만든 작업 ID |
| period | String | O | 처리 월 |
| size | Number | O | 묶인 민원 수 |
| keywords | Array | O | 핵심어 |
| exemplarIds | Array | O | 대표 민원 complaints.\_id |
| suggestedName | String | O | 제안 이름(핵심어 또는 gpt-5-mini) |
| suggestedDefinition | String / null | X | 제안 정의 |
| suggestedGroup | String / null | X | 제안 상위 그룹 |
| decision | String | O | pending, approved, held, merged |
| mergedIntoCategoryId | String / null | X | 기존 카테고리에 합친 경우 그 categoryId |
| categoryId | String / null | X | 승인으로 생긴 categoryId |
| decidedBy | String / null | X | 결정자 |
| decidedAt | Date / null | X | 결정 일시 |
| createdAt | Date | O | 생성 일시 |

인덱스: jobId, period, decision

```json
{
  "_id": "ObjectId(...)",
  "jobId": "ObjectId(...)",
  "period": "2026-08",
  "size": 24,
  "keywords": ["현수막", "불법", "가로수", "선거"],
  "exemplarIds": ["ObjectId(...)"],
  "suggestedName": "불법 현수막·광고물",
  "suggestedDefinition": "도로·가로수에 무단으로 걸린 현수막·전단",
  "suggestedGroup": "소음·건축·주거",
  "decision": "pending",
  "mergedIntoCategoryId": null,
  "categoryId": null,
  "decidedBy": null,
  "decidedAt": null,
  "createdAt": "2026-09-01T00:30:00.000Z"
}
```

**category\_mappings** — 병합·분할 이력

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 이력 ID |
| type | String | O | merge, split |
| fromCategoryId | String | O | 이전 categoryId |
| toCategoryId | String | O | 새 categoryId |
| fromVersion | Number | O | 변경 전 체계 버전 |
| toVersion | Number | O | 변경 후 체계 버전 |
| reason | String / null | X | 변경 사유 |
| createdBy | String | O | 변경한 사람 |
| createdAt | Date | O | 변경 일시 |

인덱스: fromCategoryId, toCategoryId, toVersion

**issues** — 같은 장소·같은 문제의 반복 민원 묶음

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 이슈 ID |
| categoryId | String | O | 대표 카테고리 |
| place | String | O | 대표 장소 표현 |
| location | Object / null | X | GeoJSON Point |
| complaintCount | Number | O | 묶인 민원 수 |
| firstReceivedAt | Date | O | 최초 접수일 |
| lastReceivedAt | Date | O | 최근 접수일 |
| summary | String | O | 대표 문장(묶음 중심에 가장 가까운 마스킹본) |
| maxUrgency | String | O | 묶음 안 최고 긴급도 |
| status | String | O | open, resolved |
| createdAt | Date | O | 생성 일시 |
| updatedAt | Date | O | 최종 수정 일시 |

인덱스: categoryId, place, lastReceivedAt, location(2dsphere)

**reviews** — 검토 대기와 검토 결과

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 검토 ID |
| complaintId | ObjectId | O | 대상 민원 |
| jobId | ObjectId | O | 검토 대상을 만든 작업 ID |
| reason | String | O | ambiguous, urgent, attributeUncertain |
| suggestedCategoryId | String / null | X | 시스템이 제안한 카테고리 |
| reviewedCategoryId | String / null | X | 검토자가 확정한 카테고리 |
| status | String | O | pending, done |
| reviewedBy | String / null | X | 검토자 |
| reviewedAt | Date / null | X | 검토 일시 |
| createdAt | Date | O | 생성 일시 |

인덱스: status, createdAt, jobId

### 7.4 complaints 스키마

| 필드 | MongoDB 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| \_id | ObjectId | 자동 | 민원 문서 ID |
| fileId | ObjectId | O | 원본 파일 ID |
| jobId | ObjectId | O | 만든 작업 ID |
| sourceLocator | Object | O | 원문 위치. CSV·XLSX {row}, PDF {page, block}, DOCX {paragraph, tableRow} |
| extractionConfidence | Number / null | X | OCR·분리 신뢰도(0\~1). 텍스트 추출이면 null |
| receivedAt | Date / null | X | 접수일(원본에 없으면 null) |
| period | String | O | 처리 월(예: 2026-10) |
| region | String / null | X | 행정동 |
| location | Object / null | X | GeoJSON Point(2dsphere 인덱스) |
| contentMasked | String | O | 마스킹된 본문 |
| contentType | String | O | body, title |
| sourceCategory | String / null | X | 기관이 붙여 온 기존 분류값(5.4) |
| categoryId | String / null | X | 배정된 카테고리. 미배정이면 null |
| group | String / null | X | 상위 그룹 |
| taxonomyVersion | Number | O | 배정에 쓴 체계 버전 |
| assignStatus | String | O | auto, ambiguous, unassigned, confirmed |
| similarity | Number / null | X | 배정 카테고리와의 유사도 |
| runnerUpCategoryId | String / null | X | 2위 카테고리 |
| candidateId | ObjectId / null | X | 미배정 건이 속한 신규 후보(category\_candidates.\_id) |
| attributes | Object | O | 속성 6종 {facility, place, urgency, repeat, requestType, affected}. 근거 없으면 각 값 null |
| needsReview | Boolean | O | 검토 필요 여부 |
| reviewReason | String / null | X | ambiguous, urgent, attributeUncertain |
| issueId | ObjectId / null | X | 소속 이슈(issues.\_id) |
| analyzer | String | O | 분석기·임베딩 모델 버전 |
| status | String | O | active, deleted |
| createdAt | Date | O | 생성 일시 |
| updatedAt | Date | O | 최종 수정 일시 |

인덱스: fileId, jobId, period, categoryId, assignStatus, region, issueId, location(2dsphere)

```json
{
  "_id": "ObjectId(...)",
  "fileId": "ObjectId(...)",
  "jobId": "ObjectId(...)",
  "sourceLocator": { "row": 12 },
  "extractionConfidence": null,
  "receivedAt": "2026-07-14T00:00:00.000Z",
  "period": "2026-07",
  "region": "수청동",
  "location": null,
  "contentMasked": "수청동 놀이터 옆 가로등이 밤마다 깜빡입니다.",
  "contentType": "body",
  "sourceCategory": null,
  "categoryId": "CAT-0012",
  "group": "안전·조명",
  "taxonomyVersion": 3,
  "assignStatus": "auto",
  "similarity": 0.84,
  "runnerUpCategoryId": "CAT-0031",
  "candidateId": null,
  "attributes": { "facility": "가로등", "place": "놀이터 옆", "urgency": "보통", "repeat": false, "requestType": "수리", "affected": "어린이" },
  "needsReview": false,
  "reviewReason": null,
  "issueId": "ObjectId(...)",
  "analyzer": "bge-m3@1 / gpt-5-mini-1",
  "status": "active",
  "createdAt": "2026-08-01T02:00:00.000Z",
  "updatedAt": "2026-08-01T02:00:00.000Z"
}
```

개별 민원의 임베딩 벡터는 문서에 넣지 않고 산출물 파일로 보관한다(processing\_results.analysis.artifacts). 카테고리 중심 벡터는 개수가 적어 `categories.centroid`에 둔다. 벡터 검색이 필요해지면 Atlas Vector Search를 검토한다.

### 7.5 API

기준은 `api_명세서.hwpx`이다. 아래 세 표와 안내 문구(※), 예시 JSON은 명세서 **원문 그대로**다. 명세서에 없는 내용은 맨 끝의 "명세서 외 추가 제안"으로 따로 둔다.

**파일 관리 API**

| Method | Endpoint | 기능 |
| --- | --- | --- |
| POST | /api/v1/files | 파일 업로드 및 처리 작업 생성 |
| GET | /api/v1/files | 파일 목록 및 검색 |
| GET | /api/v1/files/{file\_id} | 파일 상세 조회 |
| DELETE | /api/v1/files/{file\_id} | 파일 및 관련 결과 삭제 |
| GET | /api/v1/files/{file\_id}/preview | 원본 데이터 미리보기 |

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

fileId는 UUID 또는 MongoDB의 \_id를 문자열로 반환

**처리 정제 API**

| Method | EndPoint | 기능 |
| --- | --- | --- |
| POST | /api/v1/files/{file\_id}/process | 데이터 추출·진단 작업 시작 |
| GET | /api/v1/jobs/{job\_id} | 작업 상태 및 오류 확인 |
| GET | /api/v1/files/{file\_id}/quality | 데이터 품질 진단 결과 |
| POST | /api/v1/files/{file\_id}/cleaning-suggestions | AI 정제 규칙 추천 |
| POST | /api/v1/files/{file\_id}/clean | 승인한 정제 규칙 적용 |
| GET | /api/v1/files/{file\_id}/versions | 정제 버전 및 이력 조회 |

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

**분석·AI·다운로드 API**

| Method | EndPoint | 기능 |
| --- | --- | --- |
| POST | /api/v1/files/{file\_id}/analysis | 분석 실행 |
| GET | /api/v1/files/{file\_id}/analysis | 분석 결과 조회 |
| POST | /api/v1/files/{file\_id}/report | AI 분석 리포트 생성 |
| GET | /api/v1/dashboard/summary | 전체 통계 조회 |
| GET | /api/v1/files/{file\_id}/export?format=확장자 | 정제 결과 다운로드 |

※ 모든 API에서 성공·실패 응답 규격을 통일하세요. 작업이 오래 걸릴 수 있는 경우에는 요청을 받은 즉시 결과를 반환하기보다 작업 ID를 발급하고, 프론트엔드가 작업 상태를 조회하도록 구성하는 편이 안전

**민원 분석에서 명세서 API를 쓰는 방식**

새 엔드포인트 없이 명세서 API만으로 진행할 수 있는 범위다.

| 단계 | 명세서 API |
| --- | --- |
| 원본 업로드(CSV·XLSX·PDF·DOCX 등) | POST /api/v1/files |
| 추출 결과·민원 분리 확인(7.6) | GET /api/v1/files/{file\_id}/preview |
| 추출·품질 진단 | POST /api/v1/files/{file\_id}/process → GET /api/v1/files/{file\_id}/quality |
| 정제(경도·위도 정리, 중복 제거 등) | POST /api/v1/files/{file\_id}/cleaning-suggestions → POST /api/v1/files/{file\_id}/clean → GET /api/v1/files/{file\_id}/versions |
| 민원 분석(마스킹·배정·발견·속성·이슈) | POST /api/v1/files/{file\_id}/analysis → GET /api/v1/files/{file\_id}/analysis |
| 작업 상태 확인 | GET /api/v1/jobs/{job\_id} |
| 월간 리포트 | POST /api/v1/files/{file\_id}/report |
| 전체 통계 | GET /api/v1/dashboard/summary |
| 결과 다운로드 | GET /api/v1/files/{file\_id}/export?format=확장자 |

**정제 규칙(민원 데이터)** — 명세서의 규칙 형식(type, column / columns, strategy)을 그대로 쓴다.

| 규칙 type | 구분 | 민원 데이터 적용 사례 |
| --- | --- | --- |
| trim\_whitespace | 명세서 | 본문 앞뒤 공백·연속 공백 정리 |
| remove\_duplicates | 명세서 | 같은 민원의 중복 등록(아산 중복 3행) |
| fill\_missing | 명세서 | 접수일·행정동이 빈 행(포항 31행) 처리 |
| swap\_columns | 명세서 외 제안 | 포항 경도·위도 열 이름 뒤바뀜 |
| merge\_rows | 명세서 외 제안 | 한 민원이 여러 행으로 나뉜 경우 |
| drop\_rows\_matching | 명세서 외 제안 | 시스템·테스트 문구 |
| replace\_values | 명세서 외 제안 | 김천 처리결과 오타(과태교·과태룹 → 과태료 부과) |
| mask\_pii | 명세서 외 제안 | 실명·전화번호·상세주소(10장) |

**명세서 외 추가 제안** — 카테고리 승인·검토·민원 통계는 명세서 API로 표현할 수 없다. 명세서에 반영되기 전까지는 제안 상태이며, 명세서 작성자와 협의한다(7.7).

| Method | Endpoint | 기능 |
| --- | --- | --- |
| GET | /api/v1/complaints | 민원 목록(카테고리·기간·지역·배정 상태 필터) |
| GET | /api/v1/complaints/categories | 현재(또는 지정 버전) 카테고리 체계 |
| GET | /api/v1/complaints/categories/candidates | 이번 달 신규 후보 |
| POST | /api/v1/complaints/categories/candidates/{candidate\_id}/decision | 후보 승인·보류·기존에 합치기 |
| POST | /api/v1/complaints/categories/merge | 카테고리 병합(매핑 자동 기록) |
| POST | /api/v1/complaints/categories/split | 카테고리 분할(매핑 자동 기록) |
| GET | /api/v1/complaints/reviews | 검토 대기 목록 |
| POST | /api/v1/complaints/reviews/{review\_id} | 검토 결과 저장 |
| GET | /api/v1/complaints/stats/summary | 카테고리·월·지역·긴급도별 집계(8.1) |
| GET | /api/v1/complaints/stats/insights | 신규·급증·반복·긴급 인사이트(8.2) |
| POST | /api/v1/jobs/{job\_id}/retry | 같은 파일로 새 작업을 만들어 재처리 |

### 7.6 파일 형식별 추출(processors)

형식마다 추출기를 따로 두되, 모두 같은 결과를 돌려준다: **추출 레코드 = 본문 + 메타 필드 + 원문 위치 + 추출 신뢰도**. 그래서 마스킹부터는 원본 형식을 모른 채 똑같이 처리한다. 새 형식은 추출기 하나만 추가하면 된다.

| 형식 | 실제 형식 판별 | 추출 방법 | 민원 단위 나누기 | 주의점 |
| --- | --- | --- | --- | --- |
| CSV·TSV | 텍스트 + 구분자 감지 | pandas, 인코딩 자동 감지(UTF-8·CP949) | 1행 = 1건 | 열 이름 오류(포항 경도·위도 뒤바뀜), 메타 없는 행, 여러 행으로 쪼개진 민원 |
| XLSX·XLS | ZIP(OOXML) / OLE 서명 | openpyxl / xlrd | 1행 = 1건, 시트 선택 | 병합 셀, 여러 줄 머리글, 숨김 시트 |
| DOCX | ZIP + word/document.xml | python-docx(문단·표) | 표가 있으면 표의 행, 없으면 반복 서식 또는 1파일 = 1건 | 변경 내용 추적·메모, 머리글·바닥글, 그림 속 글자 |
| PDF(텍스트형) | %PDF + 텍스트 층 있음 | pdfplumber / PyMuPDF(텍스트·표) | 표의 행 또는 반복 서식. 페이지 경계에서 잘린 건은 이어 붙임 | 2단 배치, 머리말·쪽번호·인쇄 정보 제거 |
| PDF(스캔형)·이미지 | 텍스트 층 없음 / JPEG·PNG 서명 | 300dpi로 렌더링 후 OCR(Tesseract 한국어, 로컬) | 위와 같음 | 오인식. 국민신문고 500건은 OCR만으로 약 140건(28%)을 고쳐야 했음 → 신뢰도 낮은 건은 사람 검수 |
| TXT | 텍스트 | 그대로 | 빈 줄·구분선 기준 | 인코딩 |
| HWP·HWPX(후보) | OLE(HWP) / ZIP(HWPX) | HWPX는 XML 파싱, HWP는 변환 도구 필요 | DOCX와 같음 | 지원 우선순위는 미결정 |

**문서 형태에서 민원을 나누는 순서**

1. 표를 찾으면 표의 행 = 민원 1건으로 본다.
2. 표가 없으면 소스별 반복 서식 규칙을 적용한다. 규칙은 "시작 표지 + 필드 표지"로 설정한다(예: `번호`로 시작, `제목`·`처리 기관`·`등록일` 필드).
3. 서식이 없으면 파일 1개 = 1건으로 본다.
4. 위 방법으로 나눌 수 없으면 gpt-5-mini가 경계를 제안하고, 사람이 확인한 뒤 그 결과를 해당 소스의 규칙으로 저장한다.

업로드 뒤에는 **추출 미리보기**(`GET /api/v1/files/{file_id}/preview`)로 앞 20건과 나눈 기준을 보여주고, 담당자가 매핑·분리 규칙을 확정한 뒤 분석 작업을 만든다. 표 인식과 OCR이 더 정확한 Azure AI Document Intelligence도 대안이지만, 원본 문서가 마스킹 전에 외부로 나가므로 보안 승인 뒤에만 쓴다.

### 7.7 설계 파일 검토 메모

PRD는 설계 파일 3개를 그대로 따른다. 아래는 설계 파일 작성자에게 확인이 필요한 점이며, 답이 오기 전까지 PRD를 바꾸지 않는다.

**설계 파일 안에서 표와 예시가 다른 곳** (PRD는 표를 따름)

| 파일 | 표 | 예시 JSON | 확인할 것 |
| --- | --- | --- | --- |
| processing\_jobs.xlsx | field | fileId | 대상 파일 ID의 필드명 |
| processing\_jobs.xlsx | createAt | createdAt | 생성 시간 필드명(files는 createdAt) |
| processing\_results.xlsx | field | (예시에 없음) | processing\_jobs와 같은 이름으로 할지 |
| processing\_results.xlsx | createAt | (예시에 없음) | 위와 같음 |
| processing\_results.xlsx | \_id 설명 "작업 ID" | — | 결과 ID의 오기인지 |
| processing\_results.xlsx | quality·analysis·cleaning·ai 필수 O | 비고 "필요한 경우에만 저장" | 필수 여부 |

구현 시 주의: 민원 전용 컬렉션은 fileId·createdAt을 쓰므로, 위 항목이 표 그대로(field, createAt) 확정되면 컬렉션마다 파일 ID·생성일 필드명이 달라진다.

**설계 파일에 없어 반영하지 않은 것**

| 항목 | 영향 | 현재 처리 |
| --- | --- | --- |
| 재처리 횟수·원래 작업(`CLAUDE.md` 요구) | 재처리 이력을 작업에서 바로 볼 수 없음 | 같은 파일의 작업 목록으로 확인 |
| 대용량 결과 경로 전용 필드(`CLAUDE.md` 요구) | — | processing\_results.analysis.artifacts에 담음 |
| 작업 파라미터(처리 월·체계 버전) | 특정 월·버전을 지정해 다시 돌릴 수 없음 | 접수일로 계산, 최신 버전 사용 |
| 파일 내용 해시 | 같은 파일 중복 업로드를 막을 수 없음 | 하지 않음 |
| 인덱스 정의(`CLAUDE.md` 요구) | — | 7.3에 기준 필드만으로 제안 |
| files.status 값 목록 | 상태 이름이 구현마다 달라질 수 있음 | 미정 |

**API 명세서 관련**

| 항목 | 내용 |
| --- | --- |
| 응답 공통 형식 | 명세서는 "성공·실패 응답 규격을 통일"하라고만 하고, 등록 응답 예시는 감싸지 않은 단순 형식이다. 실패 응답의 형식(오류 코드·메시지 위치) 확정 필요 |
| fileType | 등록 응답의 fileType이 files 스키마에 없다. extension과 category 중 무엇인지 |
| 등록 응답 status | 예시의 "queued"는 processing\_jobs.status 값이다. files.status와 같은 값 목록을 쓸지 |
| fileId 형식 | "UUID 또는 MongoDB \_id 문자열" 중 하나로 확정 |
| 정제 규칙 목록 | 명세서 예시는 3종(trim\_whitespace, remove\_duplicates, fill\_missing). 민원 데이터에는 5종이 더 필요(7.5) |
| analysis 요청 본문 | 분석 종류(민원 분석 등)를 지정하는 방법이 명세서에 없다 |
| 민원 전용 API·재처리 API | 명세서에 없음. 7.5 "명세서 외 추가 제안" 11개 반영 여부 |
| DELETE /files/{file\_id} | "관련 결과"의 범위. complaints·issues·reviews 포함 여부, 카테고리 체계는 유지할지 |
| export 대상 | 명세서는 "정제 결과 다운로드". 민원 분석 결과도 같은 API로 내려받을지 |
| 인증·권한 | 명세서에 없음. 12장에서 결정 |

## 8. 통계·인사이트 지표

대시보드는 기본 집계 5개와 인사이트 4개를 보여준다. 인사이트는 "무엇을 봐야 하는가"를 골라주는 것이 목적이다.

### 8.1 기본 집계

| 지표 | 정의 | 표시 |
| --- | --- | --- |
| 카테고리별 건수 | 상위 그룹·카테고리별 민원 수와 비율 | 가로 막대, 그룹 클릭 시 카테고리 |
| 월별 추이 | 월별 건수, 상위 그룹별. 병합·분할된 카테고리는 매핑표로 최신 체계에 맞춰 재집계 | 선 그래프 |
| 지역별 분포 | 행정동별 건수, 좌표가 있으면 지도 점 | 표 + 지도 |
| 긴급도 분포 | 긴급도 4단계 건수, 카테고리별 | 누적 막대 |
| 배정 상태 | 자동 배정 / 애매 / 미배정 / 사람 확정 건수와 비율 | 수치 카드 |

### 8.2 인사이트

| 인사이트 | 규칙 | 보여주는 것 |
| --- | --- | --- |
| 이달의 신규 카테고리 | 이번 달 승인된 카테고리 | 이름, 건수, 대표 민원, 어느 그룹에 들어갔는지 |
| 신규 후보(승인 대기) | 미배정 풀에서 5건 이상 또는 2개월 연속 묶인 군집 | 제안 이름, 핵심어, 대표 민원, 승인 버튼 |
| 급증 카테고리 | 이번 달 건수가 직전 3개월 평균의 2배 이상, 최소 5건 | 증가율과 대표 민원 3건 |
| 반복 이슈 | 같은 issue\_id에 3건 이상 또는 재신고 표현 포함 | 장소·카테고리·건수·기간, 건수 순 |
| 긴급·취약계층 | 긴급도 = 긴급 또는 영향 대상 = 어린이·노인·장애인 | 우선 확인 목록 |
| 체계 건강도 | 미배정 비율, 애매 비율의 월별 추이 | 미배정이 계속 늘면 체계 정리 시점이라는 신호 |

급증·반복 기준값(2배, 3건, 5건)은 초기값이며, 실제 데이터 규모를 본 뒤 설정에서 조정한다. 지역 비교는 절대 건수로만 표시하고, 인구 등 분모가 없으면 발생률로 해석하지 않는다.

## 9. 평가 기준과 목표 성능

평가는 "데이터에서 카테고리를 제대로 찾는가"와 "다음 달 민원을 제대로 배정하는가" 두 가지다. 가상민원 1,000건은 정답 대분류 25개와 가상 접수일이 있어, 월별 운영을 그대로 재현해 볼 수 있다. 실제 적합성은 포항 116건 + 국민신문고 표본 100건에 사람이 라벨을 붙여 확인한다.

**월별 운영 시뮬레이션**: 가상민원을 접수월로 나누되, 첫 달에는 25개 유형 중 일부(예: 18개)만 들어 있게 자른다. 다음 달에 나머지 유형이 신규 카테고리로 찾아지는지, 기존 유형은 기존 카테고리로 들어가는지 확인한다.

| 항목 | 지표 | 목표(초기 가설) |
| --- | --- | --- |
| 발견 품질 | 군집과 정답 대분류의 일치도(ARI, NMI), 군집 순도 | 순도 0.80 이상, 정답 유형의 80% 이상이 군집 1개 이상으로 나타남 |
| 배정 정확도 | 자동 배정된 건 중 맞게 들어간 비율 | 0.90 이상 |
| 신규 탐지율 | 다음 달에 처음 등장한 유형이 기존 카테고리로 흡수되지 않고 신규 후보로 나온 비율 | 0.80 이상 |
| 미배정·애매 비율 | 전체 중 사람이 봐야 하는 비율 | 25% 이하 |
| 속성 | 속성별 정확도(긴급도는 1단계 차이까지 별도 표기) | 장소·대상시설 0.80, 긴급도 0.70 |
| 이슈 묶기 | 표본 50쌍의 묶음 판정 일치율 | 0.85 이상 |
| 추출·분리 | 문서형 파일에서 민원 경계를 맞게 나눈 비율, OCR 결과를 고쳐야 하는 건의 비율. 국민신문고 PDF 10개의 추출 결과를 교정본 CSV(500건)와 대조 | 분리 0.95 이상, OCR 교정 필요 10% 이하(나머지는 사람 검수) |
| 통계·급증(실데이터) | 아산 17개월 시계열에서 급증 규칙이 계절 정점(7월)을 오탐하는 비율, 김천 10월 급증 탐지 여부, 집계 건수와 원본 행 수 일치 | 건수 불일치 0, 김천 10월 탐지, 계절 오탐은 전년 동월 비교로 제거 |
| 정형 문구 규칙 | 김천 제목에서 위반 장소 유형을 맞게 뽑은 비율, 처리결과 오타 변형을 표준값으로 바꾼 비율 | 0.98 이상 |

목표치는 가설이다. M2에서 기준선을 잰 뒤 그 수치를 보고 확정한다. 분석기·체계가 바뀔 때마다 같은 고정 평가셋으로 이전 버전과 비교한다.

라벨링 기준: 2명이 독립으로 붙이고, 일치하지 않는 건은 논의해 확정한다. 사람 간 일치율을 함께 기록해, 모델 성능의 상한으로 참고한다. 1명뿐이면 표본 20%를 1주 뒤 다시 라벨링해 일관성을 확인한다.

## 10. 개인정보·보안·비기능 요구사항

민원 원문에는 이름·연락처·주소가 섞여 있다고 가정한다. 마스킹은 모든 분석보다 먼저 실행하고, 마스킹되지 않은 원문은 분석기에 넘기지 않는다.

### 마스킹 대상

| 대상 | 방법 | 치환 예 |
| --- | --- | --- |
| 전화번호 | 정규식 | `[전화]` |
| 주민등록번호·계좌번호 | 정규식 | `[식별번호]` |
| 이메일 | 정규식 | `[이메일]` |
| 차량번호 | 정규식 | `[차량]` |
| 사람 이름 | 개체명 인식 + 직함 패턴("○○○ 주무관") | `[인명]` |
| 상세 주소(번지·호수) | 정규식, 행정동까지는 남김 | `수청동 [상세주소]` |

- MongoDB Atlas에는 **마스킹본만** 저장한다. 마스킹 전 원문은 원본 파일에만 있고, DB에는 그 위치만 기록한다.
- Atlas는 서울 리전(ap-northeast-2)에 두고, IP 접근 목록·TLS·최소 권한 DB 사용자·저장 암호화를 적용한다. 실제 기관 데이터를 넣기 전에 클라우드 저장에 대한 보안 검토를 받는다.
- DB 연결 문자열과 AI API 키는 `.env`에만 두고 코드에 넣지 않는다. `.env` 계열 파일은 커밋하지 않는다.
- LLM(Azure OpenAI)에는 마스킹본만 전송하고, 전송 건수·시각을 processing\_results.ai에 남긴다. 실제 기관 민원을 보내기 전에 배포 유형을 Standard(지역)로, 리전을 Korea Central로 맞춘다. Global Standard는 국외 데이터센터에서 처리될 수 있어 가상·공개 데이터에만 쓴다.
- 업로드는 확장자와 실제 형식을 함께 검사하고, 크기 제한을 둔다. 매크로가 든 문서(docm·xlsm 등)는 거부하고, 압축 기반 형식(DOCX·XLSX·HWPX)은 풀었을 때의 크기와 파일 수를 제한해 압축 폭탄을 막는다. PDF는 페이지 수를 제한한다. 문서 속 이미지에도 개인정보가 있을 수 있으므로 OCR 결과도 마스킹 대상이다.
- 마스킹 누락을 막기 위해 표본 100건을 사람이 점검하고 누락률을 기록한다.

### 비기능 요구사항

- **재현성**: 같은 입력·설정·분석기·체계 버전이면 같은 결과가 나온다. 작업마다 설정 해시와 버전을 기록한다.
- **처리량**: 로컬 임베딩 기준 1만 건을 일반 서버(CPU)에서 30분 안에 처리한다. 긴 작업은 백그라운드로 돌고 API는 즉시 작업 ID를 돌려준다.
- **내결함**: 한 행의 오류는 행 단위로 기록하고 작업을 멈추지 않는다. 파일을 읽지 못하면 작업을 failed로 두고 원인을 남긴다. 오류는 `AppError`로 던져 라우터에서 처리한다.
- **추적성**: 모든 통계 숫자에서 근거가 된 민원(파일·행 번호)까지 내려갈 수 있다.
- **개발 규칙**: 새 기능에는 테스트를 추가하고, 작업을 끝내기 전 lint·typecheck·test를 모두 통과한다. 커밋 메시지는 `feat:`, `fix:`, `refactor:` 접두사를 쓴다.
- **실행 환경**: Python 3.11+, FastAPI, MongoDB Atlas. 임베딩 모델은 서버에 미리 받아 두고 로컬에서 돌린다.

## 11. 단계별 마일스톤

M2 시뮬레이션에서 기준값과 분석 방식을 정한 뒤에야 실제 데이터로 카테고리 v1을 만든다. 기간은 아직 정하지 않았다.

> [그림: 단계별 마일스톤 · 5단계, 게이트 1개 — 원본 PRD 문서/Word 파일 참고]

| 단계 | 산출물 |
| --- | --- |
| M1 | FastAPI 뼈대(app/ 구조), 파일 업로드·형식 검증, CSV·XLSX·DOCX·텍스트 PDF·TXT processors, 마스킹 서비스, MongoDB 컬렉션·인덱스, 실제 평가셋 CSV, 가상민원 라벨 점검 메모 |
| M2 | 시뮬레이션 리포트(9장 지표), 아산·김천 실데이터 통계·급증 검증, 국민신문고 PDF OCR 정확도, 확정 기준값 설정 파일 |
| M3 | 스캔 PDF·이미지 OCR 추출기와 검수 흐름, categories 컬렉션 v1, 첫 달 분석 결과와 리포트 |
| M4 | 작업 API(생성·조회·재처리), category\_candidates 승인 흐름, 대시보드 |
| M5 | 병합·분할 도구, category\_mappings 이력, 재집계 스크립트 |

## 12. 미결정 사항과 리스크

### 미결정 사항

- [ ] 속성 추출에 gpt-5-mini를 쓸지, 규칙 방식으로 충분한지 (M2 비교 결과로 결정)
- [ ] Azure OpenAI 배포 유형·리전 확인: gpt-5-mini-1이 Standard + Korea Central인지. 아니면 실제 민원 투입 전에 재배포
- [ ] 배정 기준 유사도와 신규 카테고리 최소 규모 확정 (시뮬레이션으로 조정)
- [ ] 카테고리 승인 권한: 누가, 언제(예: 매월 초 3일 이내) 승인하는지
- [ ] 첫 달 데이터 규모: 군집화가 의미 있려면 최소 몇 건이 필요한지
- [ ] 복수 카테고리 허용 여부: 한 민원에 문제가 둘일 때
- [ ] 원본 파일·산출물 저장소: 서버 로컬 디스크인지 객체 저장소(S3 등)인지. 저장소 인터페이스로 감싸 두고 개발은 로컬에서 시작(files.storage.provider와 일치)
- [ ] 백그라운드 작업 방식: MongoDB 기반 작업 큐 + 별도 워커로 시작(권장), 규모가 커지면 Celery 등으로 전환
- [ ] Atlas 사용 승인: 실제 민원(마스킹본)을 클라우드 DB에 두는 것에 대한 기관 보안 검토(CSAP 등). 승인 전까지 Atlas 전용 기능은 쓰지 않음
- [ ] DataLens 안에서 민원 분석의 위치: 공통(파일·작업·마스킹·임베딩)과 민원 전용(카테고리·이슈·통계)의 경계, 여러 기관 사용 시 `orgId` 도입 여부
- [ ] 설계 파일 작성자 확인(7.7): 표와 예시의 불일치(field/fileId, createAt/createdAt, 결과 \_id 설명, 필수 여부)와 설계 파일에 없는 항목(재처리 이력, 파일 해시, 작업 파라미터, files.status 값)
- [ ] API 인증 방식과 권한(업로드, 카테고리 승인, 검토, 삭제)
- [ ] HWP·HWPX 지원 여부와 시기: 공공기관 원본에 많음. HWPX는 XML로 바로 읽을 수 있고, HWP는 변환 도구가 필요
- [ ] OCR 엔진: 로컬 Tesseract로 시작할지, 보안 승인 뒤 Azure AI Document Intelligence를 쓸지
- [ ] 문서형 파일의 민원 분리 규칙을 누가 확정하고 관리하는지(소스별 설정)
- [ ] 대시보드 형태: 이 백엔드 API를 읽는 별도 프론트엔드(디자인은 DESIGN.md 참고)
- [ ] 실제 운영 데이터 확보 경로(국민신문고 본문, 지자체 게시판 등)

### 리스크

| 리스크 | 영향 | 대응 |
| --- | --- | --- |
| 실제 원문이 116건뿐 | 현실 성능을 믿기 어려움 | 평가는 실제 평가셋 기준, 원문 추가 확보를 병행 |
| 가상민원 문장이 단순 | 로컬 분류기가 템플릿에 과적합 | 가상민원 성능은 참고용으로만 보고, 실제 평가셋과 차이를 기록 |
| 국민신문고는 제목만 있음 | 속성 추출 불가, 분류 근거 부족 | 대분류·유형 발견에만 사용 |
| 마스킹 누락 | 개인정보 노출 | 표본 점검, LLM 전송 전 이중 검사 |
| 분류 체계 변경 | 이전 통계와 비교 불가 | 체계 버전을 행마다 기록, 재분류 스크립트 제공 |
| 신규 카테고리 남발 | 카테고리가 수백 개로 늘어 통계가 흩어짐 | 최소 규모 규칙, 상위 그룹 집계, 분기 병합 |
| 카테고리 의미 변질 | 매달 비슷한 건이 붙으며 정의가 넓어짐 | 승인 시점 중심 고정, 대표 민원 기준 점검 |
| 첫 달 데이터 편중(계절성) | 초기 체계에 계절 유형이 빠짐 | 신규 추가로 보완, 1년 뒤 체계 전체 점검 |
