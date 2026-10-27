"""민원 분석 작업(processing_jobs.type = complaint_analysis, PRD 4장·7.2) 처리기.

단계: validating → parsing(추출·민원 분리) → masking → embedding → attributes(속성 6종)
→ assigning(현재 체계에 배정) → discovering(미배정 풀 군집화·후보) → issue_grouping
→ statistics → saving.
- 미배정 풀은 이번 파일의 미배정 민원 + 다른 파일에서 남은 미배정 민원이다(PRD 5.1).
  결정 전(pending) 후보는 새 후보로 바뀌고, 결정된 후보는 이력으로 남는다.
- 같은 파일을 다시 분석하면 이전 민원은 deleted가 된다(저장 단계에서 바꿔 중간 실패에 안전).
- 민원별 임베딩은 산출물 파일로 둔다(PRD 7.4). 결과 요약은 processing_results.analysis.complaint.
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime

import numpy as np
from bson import ObjectId
from pydantic import JsonValue

from app.core.analysis_config import AttributeConfig, load_analysis_config
from app.core.config import Settings
from app.core.errors import AppError
from app.processors.splitter import load_split_rules
from app.repositories.candidates import CandidateRepository
from app.repositories.categories import CategoryRepository
from app.repositories.complaints import ComplaintRepository
from app.repositories.issues import IssueRepository
from app.repositories.results import ResultRepository
from app.repositories.reviews import ReviewRepository
from app.schemas.complaint import (
    AssignStatus,
    CandidateDocument,
    ComplaintAttributes,
    ComplaintDocument,
    GeoPoint,
    ReviewDocument,
    ReviewReason,
)
from app.schemas.complaint_analysis import Attributes, Category, ComplaintRecord
from app.schemas.complaint_api import ComplaintAnalysisRequest
from app.schemas.result import QualityIssue, QualityResult, ResultDocument
from app.services.artifacts import job_params_key
from app.services.assignment import assign
from app.services.attributes import extract_by_llm, extract_by_rules, refine_with_prototypes
from app.services.complaint_extraction import RawComplaint, extract_complaints
from app.services.complaint_vectors import load_vectors, save_vectors
from app.services.discovery import discover
from app.services.embedding import Embedder, Vectors, get_embedder
from app.services.issue_grouping import IssueInput, group_issues
from app.services.job_runner import JobContext
from app.services.llm import LlmClient, create_llm_client, llm_configured
from app.services.masking import mask_text
from app.services.naming import NameSuggestion, suggest_name
from app.services.ocr_setup import ocr_options

# 새 후보와 이전 후보의 소속 민원이 이 비율(Jaccard) 이상 겹치면 이름 초안을 이어 쓴다
REUSE_OVERLAP = 0.5
# 배정기(assignment.py)의 상태 → complaints.assignStatus 값. 자동 배정은 사람 확정과 구분해
# "auto"로 둔다(PRD 8.1 배정 상태: 자동 배정 / 애매 / 미배정 / 사람 확정)
STATUS_MAP: dict[str, AssignStatus] = {
    "assigned": "auto",
    "ambiguous": "ambiguous",
    "unassigned": "unassigned",
}


def _period(received: datetime | None, fallback: str) -> str:
    """처리 월(접수일의 YYYY-MM, 접수일이 없으면 작업 월).

    파라미터:
        received: 접수일
        fallback: 접수일이 없을 때 쓸 월
    """
    return f"{received.year:04d}-{received.month:02d}" if received else fallback


def _review_reason(
    attributes: Attributes,
    status: AssignStatus,
    confidence: float | None,
    low_confidence: float,
    uncertain: bool,
) -> ReviewReason | None:
    """검토 사유 하나(우선순위: 긴급 > 애매 배정 > OCR 신뢰도 낮음 > 속성 불확실).

    파라미터:
        attributes: 추출한 속성
        status: 배정 상태
        confidence: OCR·분리 신뢰도
        low_confidence: OCR 검수 기준
        uncertain: 속성 추출이 실패해 규칙으로 대신했는지
    """
    # 검토 목록에는 사유가 하나만 붙으므로, 늦게 보면 안 되는 것부터 고른다.
    # 긴급(인명·사고 위험)은 배정이 맞아도 사람이 먼저 확인해야 한다(PRD 8.2 긴급·취약계층)
    if attributes.urgency == "긴급":
        return "urgent"
    if status == "ambiguous":
        return "ambiguous"
    # OCR 신뢰도가 낮으면 본문 자체가 틀렸을 수 있어 배정·속성 모두 믿기 어렵다
    if confidence is not None and confidence < low_confidence:
        return "ocrUncertain"
    if uncertain:
        return "attributeUncertain"
    return None


def _reuse_suggestion(
    members: set[str],
    previous: dict[ObjectId, CandidateDocument],
    previous_members: dict[ObjectId, set[str]],
) -> NameSuggestion | None:
    """소속 민원이 REUSE_OVERLAP 이상 겹치는 이전 결정 전 후보의 이름 초안. 없으면 None.

    파일마다 미배정 풀을 다시 군집화하므로, 같은 후보의 이름을 매번 LLM에 다시 묻지 않게 한다.

    파라미터:
        members: 새 후보의 민원 _id 문자열
        previous: 이전 결정 전 후보
        previous_members: 이전 후보 _id → 민원 _id 문자열
    """
    # 기준값 이상 겹치는 이전 후보 중 가장 많이 겹치는 것 하나를 고른다
    best: CandidateDocument | None = None
    best_overlap = REUSE_OVERLAP
    for candidate_id, old in previous_members.items():
        # Jaccard = 교집합 / 합집합. 후보 크기가 달라도 0~1로 비교할 수 있다
        overlap = len(members & old) / len(members | old)
        if overlap >= best_overlap:
            best, best_overlap = previous[candidate_id], overlap
    if best is None:
        return None
    return NameSuggestion(best.suggested_name, best.suggested_definition, best.suggested_group)


def resolve_method(request: ComplaintAnalysisRequest, settings: Settings) -> str:
    """실제로 쓸 속성 추출 방식(auto는 fast 배포 연결 정보가 있으면 llm, 없으면 rule).

    파라미터:
        request: 분석 요청
        settings: 앱 설정
    """
    # LLM 연결 정보가 없어도 모든 기능이 돌도록 auto는 규칙 방식으로 물러선다
    if request.attribute_method == "auto":
        return "llm" if llm_configured(settings, "fast") else "rule"
    return request.attribute_method


async def _attributes(
    records: list[ComplaintRecord],
    method: str,
    ctx: JobContext,
    vectors: Vectors,
    embedder: Embedder,
    config: AttributeConfig,
) -> tuple[list[Attributes], set[str], LlmClient | None]:
    """속성 6종을 뽑는다. LLM 방식에서 실패·누락된 민원은 규칙으로 대신하고 불확실로 표시한다.

    규칙 속성은 비어 있는 긴급도·요청 유형을 임베딩 원형 분류로 보강한다.

    파라미터:
        records: 마스킹된 민원
        method: llm 또는 rule(resolve_method 결과)
        ctx: 작업 실행 정보
        vectors: records의 정규화된 임베딩
        embedder: 임베더(원형 문장용)
        config: 원형 분류 기준
    """
    # 규칙 속성은 LLM 방식에서도 먼저 만든다. LLM이 실패·누락한 민원의 대체값이 된다
    rules = refine_with_prototypes(
        [extract_by_rules(r) for r in records], vectors, embedder, config
    )
    by_rule = {a.record_id: a for a in rules}
    if method == "rule":
        return [by_rule[r.record_id] for r in records], set(), None
    # 속성 추출은 fast 배포를 쓴다(이름 초안은 deep). 호출 내역(usage)을 남기려고 client도 돌려준다
    client = create_llm_client(ctx.settings, "fast")
    extraction = await extract_by_llm(records, client)
    found = {a.record_id: a for a in extraction.attributes}
    # LLM 값이 없는 민원은 규칙 값으로 채우되 불확실로 표시해 검토 사유(attributeUncertain)가 된다
    uncertain = {r.record_id for r in records if r.record_id not in found}
    return [found.get(r.record_id, by_rule[r.record_id]) for r in records], uncertain, client


async def _pool_vectors(
    pool: list[ComplaintDocument], ctx: JobContext, embedder: Embedder
) -> Vectors:
    """기존 미배정 민원의 벡터(산출물 파일에서 읽고, 없으면 다시 계산).

    파라미터:
        pool: 다른 파일의 미배정 민원
        ctx: 작업 실행 정보
        embedder: 임베더(캐시 붙음)
    """
    if not pool:
        return np.zeros((0, 0), dtype=np.float32)
    # 파일 읽기는 이벤트 루프를 막지 않게 스레드에서 한다
    stored = await asyncio.to_thread(
        load_vectors, ctx.storage, [(c.id, c.file_id, c.job_id) for c in pool]
    )
    # 산출물이 지워진 민원은 마스킹본으로 다시 계산한다(임베더 캐시가 있으면 금방 끝난다)
    missing = [c for c in pool if c.id not in stored]
    if missing:
        computed = await asyncio.to_thread(embedder.embed, [c.content_masked for c in missing])
        for row, complaint in enumerate(missing):
            stored[complaint.id] = computed[row]
    return np.stack([stored[c.id] for c in pool]).astype(np.float32)


async def analyze_complaints(ctx: JobContext) -> None:
    """민원 분석 작업을 실행한다(단계는 모듈 설명 참고).

    파라미터:
        ctx: 작업 실행 정보
    """
    # ── validating: 원본과 작업 요청을 확인한다. 진행률(%)은 단계별 대략적인 소요 비중이다
    await ctx.report("validating", 5)
    path = ctx.storage.path(ctx.file.storage.key)
    if not path.is_file():
        raise AppError("SOURCE_MISSING", "원본 파일이 저장소에 없습니다.", 422)
    # 요청(매핑·분리 규칙 등)은 processing_jobs에 필드를 늘리지 않으려고 산출물 파일로 둔다
    raw = await asyncio.to_thread(ctx.storage.read_text, job_params_key(ctx.job.id))
    request = ComplaintAnalysisRequest.model_validate_json(raw)
    config = load_analysis_config()
    db = ctx.db
    complaints_repo = ComplaintRepository(db)
    categories_repo = CategoryRepository(db)
    candidates_repo = CandidateRepository(db)
    reviews_repo = ReviewRepository(db)
    issues_repo = IssueRepository(db)
    now = datetime.now(UTC)
    # 접수일이 없는 민원(문서형 등)은 작업을 돌린 달로 집계한다
    job_month = f"{now.year:04d}-{now.month:02d}"

    # ── parsing: 형식별 추출·민원 분리. OCR이 있어 오래 걸리므로 스레드에서 돌린다
    await ctx.report("parsing", 10)
    extraction = await asyncio.to_thread(
        extract_complaints,
        path,
        ctx.file.extension,
        request,
        ocr_options(ctx.settings),
        load_split_rules(),
    )
    raws: list[RawComplaint] = extraction.records
    if not raws:
        raise AppError(
            "NO_COMPLAINTS", "파일에서 민원을 찾지 못했습니다(매핑·분리 규칙 확인).", 422
        )

    # ── masking: 이후 모든 단계(임베딩·LLM·DB 저장)는 마스킹본만 본다(PRD 10장).
    #    종류별 치환 건수는 결과 요약에 남겨 마스킹 점검에 쓴다
    await ctx.report("masking", 25)
    masked = [mask_text(r.text) for r in raws]
    pii: Counter[str] = Counter()
    for result in masked:
        pii.update(result.counts)
    # complaints._id를 미리 만들어, 저장 전 단계(배정·후보·이슈·벡터 파일)에서 같은 id로 잇는다
    docs_ids = [ObjectId() for _ in raws]
    records = [
        ComplaintRecord(record_id=str(oid), text=m.text, month=_period(r.received_at, job_month))
        for oid, m, r in zip(docs_ids, masked, raws, strict=True)
    ]

    # ── embedding: 로컬 모델로만 계산한다(원문이 밖으로 나가지 않음, PRD 6장)
    await ctx.report("embedding", 35)
    embedder = get_embedder(ctx.settings)
    vectors = await asyncio.to_thread(embedder.embed, [r.text for r in records])

    await ctx.report("attributes", 45)
    method = resolve_method(request, ctx.settings)
    attributes, uncertain, fast_client = await _attributes(
        records, method, ctx, vectors, embedder, config.attributes
    )

    # ── assigning: 가장 최신 체계 버전의 카테고리 중심과 비교한다. 체계가 없으면(첫 달) 전부
    #    미배정이 된다
    await ctx.report("assigning", 55)
    version = await categories_repo.latest_version()
    category_docs = await categories_repo.list_version(version) if version else []
    taxonomy = [
        Category(
            category_id=c.category_id,
            name=c.name,
            centroid=c.centroid,
            created_in_version=c.created_in_version,
        )
        for c in category_docs
    ]
    groups = {c.category_id: c.group for c in category_docs}
    assignments = assign(records, vectors, taxonomy, config.assignment)

    # 민원마다 임베딩 모델과 속성 분석기를 기록한다(PRD 6장, 7.4 analyzer)
    analyzer = f"{embedder.model_name} / {attributes[0].analyzer if attributes else '-'}"
    docs: list[ComplaintDocument] = []
    for oid, raw_item, record, attr, assignment in zip(
        docs_ids, raws, records, attributes, assignments, strict=True
    ):
        status = STATUS_MAP[assignment.status]
        reason = _review_reason(
            attr,
            status,
            raw_item.confidence,
            ctx.settings.ocr_low_confidence,
            record.record_id in uncertain,
        )
        # 카테고리는 자동 배정일 때만 넣는다. 애매·미배정의 1위 후보는 similarity·검토 제안으로만
        # 남겨, 확정되지 않은 값이 통계에 섞이지 않게 한다
        assigned = status == "auto"
        # GeoJSON Point는 [경도, 위도] 순서다
        location = (
            GeoPoint(coordinates=[raw_item.longitude, raw_item.latitude])
            if raw_item.latitude is not None and raw_item.longitude is not None
            else None
        )
        docs.append(
            ComplaintDocument(
                id=oid,
                file_id=ctx.file.id,
                job_id=ctx.job.id,
                source_locator=raw_item.locator,
                extraction_confidence=raw_item.confidence,
                received_at=raw_item.received_at,
                period=record.month or job_month,
                # 원본 열·고정값의 지역이 없으면 본문에서 뽑은 행정동을 쓴다
                region=raw_item.region or attr.place.dong,
                location=location,
                content_masked=record.text,
                content_type=raw_item.content_type,
                source_category=raw_item.source_category,
                category_id=assignment.category_id if assigned else None,
                group=groups.get(assignment.category_id or "") if assigned else None,
                taxonomy_version=version,
                assign_status=status,
                similarity=assignment.top1_similarity,
                runner_up_category_id=assignment.second_category_id,
                attributes=ComplaintAttributes(
                    facility=attr.facility,
                    place=attr.place.landmark,
                    urgency=attr.urgency,
                    repeat=attr.repeat,
                    request_type=attr.request_type,
                    affected=attr.affected,
                ),
                needs_review=reason is not None,
                review_reason=reason,
                analyzer=analyzer,
            )
        )
    vector_of = {doc.id: vectors[i] for i, doc in enumerate(docs)}

    # ── discovering: 미배정 풀 전체를 다시 군집화해 신규 카테고리 후보를 만든다(PRD 4장 5단계).
    #    후보는 담당자가 승인해야 카테고리가 된다. 군집은 통계적 묶음일 뿐 업무상 의미가 맞는지
    #    기계가 보장하지 못하고, 승인 뒤에는 체계 버전과 과거 통계가 바뀌기 때문이다(PRD 5.1)
    await ctx.report("discovering", 65)
    # 결정 전 후보는 새 후보로 바뀐다. 소속이 거의 같은 새 후보는 이전 이름 초안을 이어 쓴다
    previous = {c.id: c for c in await candidates_repo.list_by("pending")}
    pool_before = await complaints_repo.unassigned_pool()
    previous_members: dict[ObjectId, set[str]] = {}
    for complaint in pool_before:
        if complaint.candidate_id in previous:
            previous_members.setdefault(complaint.candidate_id, set()).add(str(complaint.id))
    # 결정 전 후보와 민원의 연결을 지운다. 결정된(승인·보류 등) 후보는 이력이라 남긴다
    stale_candidates = await candidates_repo.delete_pending()
    await complaints_repo.clear_candidates(stale_candidates)
    # 같은 파일을 다시 분석하는 중이면 그 파일의 이전 민원은 곧 retire되므로 풀에서 뺀다
    old_pool = [c for c in pool_before if c.file_id != ctx.file.id]
    old_vectors = await _pool_vectors(old_pool, ctx, embedder)
    # 애매 건은 검토 대기로 가고 풀에 넣지 않는다. 미배정만 새 카테고리 발견 대상이다
    new_pool = [d for d in docs if d.assign_status == "unassigned"]
    pool = [*old_pool, *new_pool]
    # 빈 (0, 0) 행렬은 차원이 맞지 않아 vstack에서 빼고, pool과 같은 순서로 벡터를 쌓는다
    parts = [m for m in (old_vectors, *(vector_of[d.id][None, :] for d in new_pool)) if len(m)]
    pool_vectors = np.vstack(parts).astype(np.float32) if parts else old_vectors
    found = discover(
        [ComplaintRecord(record_id=str(c.id), text=c.content_masked, month=c.period) for c in pool],
        pool_vectors,
        config.discovery,
        embedding_model=embedder.model_name,
        id_prefix=f"CAND-{job_month}",
    )
    # 이름 초안은 deep 배포로 후보마다 한 번 부른다. 규칙 방식 작업이거나 deep 연결 정보가
    # 없으면 LLM 없이 핵심어 이름을 쓴다(외부 전송 없음)
    deep_client = (
        create_llm_client(ctx.settings, "deep")
        if method == "llm" and llm_configured(ctx.settings, "deep")
        else None
    )
    text_of = {str(c.id): c.content_masked for c in pool}
    period_of = {str(c.id): c.period for c in pool}
    candidates: list[CandidateDocument] = []
    member_of: dict[ObjectId, ObjectId] = {}
    for candidate in found:
        suggestion = _reuse_suggestion(set(candidate.member_ids), previous, previous_members)
        if suggestion is None:
            suggestion = await suggest_name(
                candidate.keywords, [text_of[i] for i in candidate.exemplar_ids], deep_client
            )
        # 후보의 처리 월은 소속 민원 중 가장 최근 달(지난달 풀에서 넘어온 민원이 섞여도 이번 달)
        candidate_doc = CandidateDocument(
            job_id=ctx.job.id,
            period=max(period_of[i] for i in candidate.member_ids),
            size=len(candidate.member_ids),
            keywords=candidate.keywords,
            exemplar_ids=[ObjectId(i) for i in candidate.exemplar_ids],
            suggested_name=suggestion.name,
            suggested_definition=suggestion.definition,
            suggested_group=suggestion.group,
        )
        candidates.append(candidate_doc)
        for member in candidate.member_ids:
            member_of[ObjectId(member)] = candidate_doc.id
    for doc in docs:
        doc.candidate_id = member_of.get(doc.id)

    # ── issue_grouping: 자동 배정된 민원만 이슈로 묶는다("같은 카테고리" 조건이 필요하므로).
    #    기존 이슈는 벡터를 따로 저장하지 않아 대표 문장을 다시 임베딩한다(캐시가 있어 가볍다)
    await ctx.report("issue_grouping", 80)
    open_issues = await issues_repo.open_issues()
    issue_vectors = (
        await asyncio.to_thread(embedder.embed, [i.summary for i in open_issues])
        if open_issues
        else np.zeros((0, 0), dtype=np.float32)
    )
    items = [
        IssueInput(
            complaint_id=d.id,
            category_id=d.category_id,
            vector=vector_of[d.id],
            text=d.content_masked,
            # 장소 표현은 "행정동 + 지물"로 만든다. 지물이 없으면 동 이름만으로는 범위가 너무
            # 넓어 같은 장소로 치지 않는다
            place=" ".join(p for p in (d.region, d.attributes.place) if p)
            if d.attributes.place
            else None,
            latitude=d.location.coordinates[1] if d.location else None,
            longitude=d.location.coordinates[0] if d.location else None,
            received_at=d.received_at or d.created_at,
            urgency=d.attributes.urgency,
        )
        for d in docs
        if d.category_id is not None
    ]
    grouped = group_issues(items, open_issues, issue_vectors, config.issue)
    for doc in docs:
        doc.issue_id = grouped.assignments.get(doc.id)

    # ── statistics: 이번 작업의 요약(processing_results.analysis.complaint). 배정 상태 비율은
    #    체계 건강도(미배정·애매 비율 추이, PRD 8.2)를 보는 근거가 된다
    await ctx.report("statistics", 90)
    total = len(docs)
    statuses = Counter(d.assign_status for d in docs)
    reasons = Counter(d.review_reason for d in docs if d.review_reason)
    periods = sorted(d.period for d in docs)
    summary: dict[str, JsonValue] = {
        "total": total,
        "assignedRate": round(statuses["auto"] / total, 4),
        "ambiguousRate": round(statuses["ambiguous"] / total, 4),
        "unassignedRate": round(statuses["unassigned"] / total, 4),
        "reviewCount": sum(reasons.values()),
        "reviewReasons": {str(k): v for k, v in reasons.items()},
        "candidateCount": len(candidates),
        "poolSize": len(pool),
        "taxonomyVersion": version,
        "period": {"from": periods[0], "to": periods[-1]},
        "attributeMethod": method,
        "issuesCreated": len(grouped.created),
        "issuesUpdated": len(grouped.updated),
        "maskedPii": dict(pii),
    }
    # 추출 단계의 품질 문제에 검토 대기 건수를 더해 quality.issues 하나로 보여 준다
    issues: list[QualityIssue] = list(extraction.issues)
    if reasons:
        issues.append(
            QualityIssue(
                type="needs_review",
                severity="medium",
                count=sum(reasons.values()),
                location=None,
                message=f"검토가 필요한 민원 {sum(reasons.values())}건이 있습니다.",
            )
        )

    # ── saving: 앞 단계는 메모리에서만 계산했다. 이전 민원 교체를 여기서 한꺼번에 해야 중간에
    #    실패해도 이전 분석 결과가 그대로 남는다(재처리는 같은 파일로 새 작업)
    await ctx.report("saving", 95)
    # 같은 파일의 이전 민원은 지우지 않고 deleted로 돌려 이력을 남기고, 걸려 있던 검토는 취소한다
    old_ids = [c.id for c in (await complaints_repo.list_filtered(file_id=ctx.file.id, limit=0))[0]]
    await complaints_repo.retire_file(ctx.file.id)
    await reviews_repo.cancel_pending_for(old_ids)
    await complaints_repo.insert_many(docs)
    await reviews_repo.insert_many(
        [
            ReviewDocument(
                complaint_id=d.id,
                job_id=ctx.job.id,
                reason=d.review_reason,
                suggested_category_id=a.category_id,
            )
            for d, a in zip(docs, assignments, strict=True)
            if d.review_reason is not None
        ]
    )
    await candidates_repo.insert_many(candidates)
    # 다른 파일에서 남아 있던 풀 민원도 새 후보에 들었으면 후보 연결을 고친다
    await complaints_repo.update_fields(
        [(c.id, {"candidateId": member_of[c.id]}) for c in old_pool if c.id in member_of]
    )
    await issues_repo.insert_many(grouped.created)
    for issue in grouped.updated:
        await issues_repo.replace(issue)
    # 민원 벡터는 MongoDB 문서가 아니라 산출물 파일로 두고 경로만 결과에 남긴다(PRD 7.4)
    vectors_storage_key = await asyncio.to_thread(
        save_vectors, ctx.storage, ctx.file.id, ctx.job.id, docs_ids, vectors
    )
    # LLM을 실제로 쓴 경우에만 호출 건수·토큰·실패를 processing_results.ai에 남긴다(PRD 6.1)
    ai: dict[str, JsonValue] = {}
    if fast_client is not None:
        ai["attributes"] = fast_client.usage.to_record()
    if deep_client is not None:
        ai["naming"] = deep_client.usage.to_record()
    results = ResultRepository(db)
    await results.insert(
        ResultDocument(
            field=ctx.file.id,
            job_id=ctx.job.id,
            result_type="analysis",
            version=await results.next_version(ctx.file.id, "analysis"),
            quality=QualityResult(issues=issues),
            analysis={
                **extraction.info,
                "complaint": summary,
                "artifacts": [{"kind": "embeddings", "storageKey": vectors_storage_key}],
            },
            ai=ai,
        )
    )
