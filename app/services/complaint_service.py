"""민원 분석 API 업무 로직: 분석 작업 등록, 민원·체계·후보 조회, 후보 결정, 검토 저장.

후보 승인(PRD 5.1·5.3): 소속 민원 벡터 평균을 중심으로 고정하고, 체계 버전을 하나 올려
기존 활성 카테고리를 새 버전으로 옮긴 뒤 새 카테고리를 더한다. 그다음 미배정 풀을 새 체계로
다시 배정한다(소급 배정). 승인 권한(PRD 12장)은 미정이라 결정자는 요청 값으로만 받는다.
"""

import asyncio

import numpy as np
from bson import ObjectId
from bson.errors import InvalidId

from app.core.analysis_config import load_analysis_config
from app.core.config import Settings
from app.core.errors import AppError
from app.processors.document import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS
from app.processors.splitter import load_split_rules
from app.processors.tabular import TABULAR_EXTENSIONS
from app.processors.text import TEXT_EXTENSIONS
from app.repositories.candidates import CandidateRepository
from app.repositories.categories import CategoryRepository
from app.repositories.complaints import ComplaintRepository
from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.reviews import ReviewRepository
from app.schemas.complaint import (
    COMPLAINT_ANALYSIS_JOB_TYPE,
    AssignStatus,
    CandidateDecision,
    CandidateDocument,
    CategoryDocument,
    ComplaintDocument,
    ReviewDocument,
)
from app.schemas.complaint_analysis import Category, ComplaintRecord
from app.schemas.complaint_api import (
    CandidateDecisionRequest,
    CandidateDecisionResult,
    ComplaintAnalysisRequest,
    ReviewDecisionRequest,
)
from app.schemas.job import JobDocument
from app.schemas.mongo import utc_now
from app.services.artifacts import job_params_key
from app.services.assignment import assign
from app.services.complaint_vectors import load_vectors
from app.services.embedding import Vectors, normalize
from app.services.file_service import parse_file_id
from app.services.llm import llm_configured
from app.services.storage import LocalStorage

# 민원 분석을 받는 확장자: 추출기(complaint_extraction)가 다루는 형식 전부
ANALYZABLE = TABULAR_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | TEXT_EXTENSIONS


def parse_object_id(value: str, code: str, message: str) -> ObjectId:
    """경로의 ID 문자열을 ObjectId로 바꾼다. 형식이 틀리면 404.

    파라미터:
        value: ID 문자열
        code: 오류 코드
        message: 오류 메시지
    """
    # 형식이 틀린 ID는 어차피 존재할 수 없는 문서이므로 422가 아니라 404로 응답한다
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as exc:
        raise AppError(code, message, 404) from exc


class ComplaintService:
    """민원 분석 API 업무 로직. 분석 자체는 워커(complaint_handler)가 한다."""

    def __init__(
        self,
        files: FileRepository,
        jobs: JobRepository,
        complaints: ComplaintRepository,
        categories: CategoryRepository,
        candidates: CandidateRepository,
        reviews: ReviewRepository,
        storage: LocalStorage,
        settings: Settings,
    ) -> None:
        """서비스를 만든다.

        파라미터:
            files: files 저장소
            jobs: processing_jobs 저장소
            complaints: complaints 저장소
            categories: categories 저장소
            candidates: category_candidates 저장소
            reviews: reviews 저장소
            storage: 원본·산출물 저장소
            settings: 앱 설정
        """
        self._files = files
        self._jobs = jobs
        self._complaints = complaints
        self._categories = categories
        self._candidates = candidates
        self._reviews = reviews
        self._storage = storage
        self._settings = settings

    async def start_analysis(self, file_id: str, request: ComplaintAnalysisRequest) -> JobDocument:
        """민원 분석 작업을 만든다. 다음 경우에는 작업을 만들지 않고 거부한다.

        - 파일이 없음(404), 끝나지 않은 작업이 있음(409), 지원하지 않는 형식(422)
        - 표 파일에 본문·제목 매핑이 없음(422), 없는 분리 규칙(422)
        - LLM 속성 추출을 골랐는데 fast 배포 연결 정보가 없음(503)

        파라미터:
            file_id: 파일 ID 문자열
            request: 분석 요청
        """
        # 워커에서 실패할 요청은 작업을 만들기 전에 여기서 걸러, 사용자가 작업 상태를 기다리지
        # 않고 바로 원인을 알 수 있게 한다
        doc = await self._files.get(parse_file_id(file_id))
        if doc is None:
            raise AppError("FILE_NOT_FOUND", "파일을 찾을 수 없습니다.", 404)
        if doc.extension not in ANALYZABLE:
            raise AppError(
                "ANALYSIS_NOT_SUPPORTED",
                f"{doc.extension} 파일은 민원 분석을 지원하지 않습니다.",
                422,
            )
        mapped = request.mapping.mapped()
        if doc.extension in TABULAR_EXTENSIONS and not ({"content", "title"} & mapped.keys()):
            raise AppError(
                "MAPPING_REQUIRED",
                "표 파일은 본문(content) 또는 제목(title) 열 매핑이 필요합니다.",
                422,
            )
        if request.split_rule is not None and request.split_rule not in load_split_rules():
            raise AppError(
                "SPLIT_RULE_NOT_FOUND", f"분리 규칙이 없습니다: {request.split_rule}", 422
            )
        if request.attribute_method == "llm" and not llm_configured(self._settings, "fast"):
            raise AppError(
                "LLM_NOT_CONFIGURED",
                "LLM 속성 추출용 연결 정보가 없습니다(.env의 AZURE_OPENAI_*_FAST 등). "
                "attributeMethod를 rule로 보내면 규칙 방식으로 분석합니다.",
                503,
            )
        # 같은 파일을 동시에 두 번 분석하면 이전 민원 교체(retire)가 서로 엉키므로 막는다
        if await self._jobs.has_active(doc.id):
            raise AppError("JOB_ALREADY_RUNNING", "이미 처리 중인 작업이 있습니다.", 409)
        # processing_jobs에는 기준 스키마에 없는 필드를 넣지 않으므로, 요청 내용은 작업 ID로
        # 찾을 수 있는 파일에 둔다. 작업보다 먼저 써야 워커가 작업을 집었을 때 파일이 있다
        job = JobDocument(field=doc.id, type=COMPLAINT_ANALYSIS_JOB_TYPE)
        await asyncio.to_thread(
            self._storage.write_text, job_params_key(job.id), request.model_dump_json()
        )
        await self._jobs.insert(job)
        await self._files.update_status(doc.id, "queued", last_job_id=job.id)
        return job

    async def list_complaints(
        self,
        *,
        category_id: str | None,
        period: str | None,
        region: str | None,
        assign_status: AssignStatus | None,
        file_id: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[ComplaintDocument], int]:
        """민원 목록과 전체 건수(활성 민원만).

        파라미터:
            category_id: 카테고리 ID 필터
            period: 처리 월 필터
            region: 행정동 필터
            assign_status: 배정 상태 필터
            file_id: 파일 ID 필터
            skip: 건너뛸 개수
            limit: 최대 개수
        """
        return await self._complaints.list_filtered(
            category_id=category_id,
            period=period,
            region=region,
            assign_status=assign_status,
            file_id=parse_file_id(file_id) if file_id else None,
            skip=skip,
            limit=limit,
        )

    async def taxonomy(self, version: int | None) -> tuple[int, list[CategoryDocument]]:
        """체계 버전과 그 버전의 카테고리(지정하지 않으면 최신). 없는 버전이면 404.

        파라미터:
            version: 체계 버전
        """
        # 체계 버전은 승인마다 1씩 올라 1..latest가 빠짐없이 있다. 아직 승인이 없으면 latest=0
        latest = await self._categories.latest_version()
        target = latest if version is None else version
        if version is not None and not 1 <= version <= latest:
            raise AppError("TAXONOMY_NOT_FOUND", f"체계 버전 {version}이 없습니다.", 404)
        return target, await self._categories.list_version(target) if target else []

    async def candidates(
        self, decision: CandidateDecision | None, period: str | None
    ) -> list[tuple[CandidateDocument, list[str]]]:
        """후보 목록과 후보별 대표 민원 본문(마스킹본).

        파라미터:
            decision: 결정 상태 필터
            period: 처리 월 필터
        """
        found = await self._candidates.list_by(decision, period)
        # 후보마다 따로 조회하지 않고 대표 민원을 한 번에 읽는다. 재분석으로 지워진 민원은 빠진다
        exemplar_ids = [i for c in found for i in c.exemplar_ids]
        texts = {c.id: c.content_masked for c in await self._complaints.get_many(exemplar_ids)}
        return [(c, [texts[i] for i in c.exemplar_ids if i in texts]) for c in found]

    async def _member_vectors(self, members: list[ComplaintDocument]) -> dict[ObjectId, Vectors]:
        """민원 벡터를 산출물에서 읽는다.

        파라미터:
            members: 민원 목록
        """
        return await asyncio.to_thread(
            load_vectors, self._storage, [(m.id, m.file_id, m.job_id) for m in members]
        )

    async def _retro_assign(self, version: int) -> int:
        """미배정 풀을 새 체계로 다시 배정한다(소급 배정). 새로 배정된 수를 돌려준다.

        파라미터:
            version: 새 체계 버전
        """
        # 잡음으로 빠졌거나 다른 후보에 들었던 풀 민원도 새 카테고리에 들어갈 수 있다(PRD 4장 6단계)
        pool = await self._complaints.unassigned_pool()
        if not pool:
            return 0
        # 벡터 산출물이 없는 민원은 비교할 수 없어 풀에 그대로 둔다(다음 분석 때 다시 계산된다)
        vectors = await self._member_vectors(pool)
        pool = [c for c in pool if c.id in vectors]
        if not pool:
            return 0
        categories = await self._categories.list_version(version)
        taxonomy = [
            Category(
                category_id=c.category_id,
                name=c.name,
                centroid=c.centroid,
                created_in_version=c.created_in_version,
            )
            for c in categories
        ]
        groups = {c.category_id: c.group for c in categories}
        results = assign(
            [ComplaintRecord(record_id=str(c.id), text=c.content_masked) for c in pool],
            np.stack([vectors[c.id] for c in pool]).astype(np.float32),
            taxonomy,
            load_analysis_config().assignment,
        )
        # 기준을 넘고 애매하지 않은 건만 고친다. 나머지는 미배정으로 남아 다음 달 발견에 다시 든다
        updates: list[tuple[ObjectId, dict[str, object]]] = []
        for complaint, result in zip(pool, results, strict=True):
            if result.status == "assigned" and result.category_id is not None:
                updates.append(
                    (
                        complaint.id,
                        {
                            "categoryId": result.category_id,
                            "group": groups.get(result.category_id),
                            "assignStatus": "auto",
                            "similarity": result.top1_similarity,
                            "runnerUpCategoryId": result.second_category_id,
                            "taxonomyVersion": version,
                        },
                    )
                )
        await self._complaints.update_fields(updates)
        return len(updates)

    async def _new_version(self, added: CategoryDocument | None) -> int:
        """기존 활성 카테고리를 새 버전으로 옮기고(added가 있으면 더해) 새 버전 번호를 돌려준다.

        파라미터:
            added: 새로 승인한 카테고리(version은 이 함수가 정한다)
        """
        # 체계는 버전마다 카테고리 전체를 새 문서로 복사해 둔다. 그래야 과거 버전 기준 통계를
        # 그 시점 체계 그대로 다시 볼 수 있다(categoryId + version 고유, PRD 5.2)
        latest = await self._categories.latest_version()
        version = latest + 1
        carried = [
            category.model_copy(update={"id": ObjectId(), "version": version})
            for category in (await self._categories.list_version(latest) if latest else [])
        ]
        if added is not None:
            carried.append(
                added.model_copy(update={"version": version, "created_in_version": version})
            )
        await self._categories.insert_many(carried)
        return version

    async def decide(
        self, candidate_id: str, request: CandidateDecisionRequest
    ) -> CandidateDecisionResult:
        """후보를 승인·보류·기존 카테고리에 합치기로 결정한다. 이미 결정된 후보면 409.

        파라미터:
            candidate_id: 후보 ID 문자열
            request: 결정 내용(결정자 포함)
        """
        oid = parse_object_id(candidate_id, "CANDIDATE_NOT_FOUND", "후보를 찾을 수 없습니다.")
        candidate = await self._candidates.get(oid)
        if candidate is None:
            raise AppError("CANDIDATE_NOT_FOUND", "후보를 찾을 수 없습니다.", 404)
        if candidate.decision != "pending":
            raise AppError("CANDIDATE_DECIDED", "이미 결정된 후보입니다.", 409)
        # 군집은 통계적 묶음일 뿐이라 업무상 한 유형인지는 사람이 판단한다. 그래서 후보는 자동으로
        # 카테고리가 되지 않고 여기서 담당자 결정을 거친다(PRD 4장 6단계)
        members = await self._complaints.by_candidate(candidate.id)
        latest = await self._categories.latest_version()
        now = utc_now()

        # 보류: 체계는 그대로 두고 결정만 기록한다. 소속 민원은 미배정으로 남는다
        if request.decision == "held":
            await self._candidates.decide(candidate.id, "held", request.decided_by, now)
            return CandidateDecisionResult(
                candidate_id=candidate_id,
                decision="held",
                category_id=None,
                taxonomy_version=latest,
                member_count=len(members),
                retro_assigned=0,
            )

        # 합치기: 기존 카테고리에 소속 민원만 배정한다. 카테고리 중심은 승인 시점 값으로 고정이라
        # 바꾸지 않고(PRD 5.3), 새 카테고리가 없으므로 체계 버전도 올리지 않는다
        if request.decision == "merged":
            target_id = request.merged_into_category_id
            target = (
                await self._categories.get_in_version(target_id, latest)
                if target_id and latest
                else None
            )
            if target is None or target.status != "active":
                raise AppError("CATEGORY_NOT_FOUND", "합칠 카테고리가 현재 체계에 없습니다.", 422)
            await self._assign_members(members, target, latest, "auto")
            await self._candidates.decide(
                candidate.id, "merged", request.decided_by, now, merged_into=target.category_id
            )
            return CandidateDecisionResult(
                candidate_id=candidate_id,
                decision="merged",
                category_id=target.category_id,
                taxonomy_version=latest,
                member_count=len(members),
                retro_assigned=0,
            )

        # 승인: 담당자가 고친 이름·그룹·정의가 있으면 그것을, 없으면 제안 초안을 쓴다.
        # 상위 그룹은 집계에 쓰이므로(PRD 5.4) 비어 있으면 승인하지 않는다
        name = request.name or candidate.suggested_name
        group = request.group or candidate.suggested_group
        if not group:
            raise AppError("GROUP_REQUIRED", "승인하려면 상위 그룹(group)이 필요합니다.", 422)
        vectors = await self._member_vectors(members)
        if not vectors:
            raise AppError(
                "VECTORS_MISSING",
                "후보 민원의 임베딩 산출물이 없어 중심을 계산할 수 없습니다.",
                409,
            )
        # 중심 = 승인 시점 소속 민원 벡터의 평균(정규화). 이후 배정된 민원으로 옮기지 않아
        # 카테고리 의미가 매달 조금씩 넓어지는 것을 막는다(PRD 5.3 중심 고정)
        matrix = np.stack(list(vectors.values())).astype(np.float32)
        centroid = normalize(matrix.mean(axis=0, keepdims=True))[0]
        # categoryId는 이름이 바뀌어도 유지되는 번호라 지금까지 쓴 가장 큰 번호 다음을 쓴다.
        # 핵심어 최대 10개·대표 민원 5건은 categories 스키마 설명을 따른다(PRD 5.2)
        category = CategoryDocument(
            category_id=f"CAT-{await self._categories.max_category_number() + 1:04d}",
            version=latest + 1,
            name=name,
            group=group,
            definition=request.definition or candidate.suggested_definition or "",
            keywords=candidate.keywords[:10],
            exemplar_ids=candidate.exemplar_ids[:5],
            centroid=[float(x) for x in centroid],
            embedding_model=self._settings.embedding_model,
            created_in_version=latest + 1,
            approved_by=request.decided_by,
            approved_at=now,
        )
        # 결정 기록은 pending일 때만 바뀐다. 두 사람이 동시에 승인하면 한쪽만 성공해, 같은 후보로
        # 카테고리가 둘 생기지 않는다
        if not await self._candidates.decide(
            candidate.id, "approved", request.decided_by, now, category_id=category.category_id
        ):
            raise AppError("CANDIDATE_DECIDED", "이미 결정된 후보입니다.", 409)
        # 새 버전 체계를 만들고, 후보 소속 민원을 배정한 뒤, 남은 풀을 새 체계로 소급 배정한다
        version = await self._new_version(category)
        await self._assign_members(members, category, version, "auto", vectors)
        retro = await self._retro_assign(version)
        return CandidateDecisionResult(
            candidate_id=candidate_id,
            decision="approved",
            category_id=category.category_id,
            taxonomy_version=version,
            member_count=len(members),
            retro_assigned=retro,
        )

    async def _assign_members(
        self,
        members: list[ComplaintDocument],
        category: CategoryDocument,
        version: int,
        status: AssignStatus,
        vectors: dict[ObjectId, Vectors] | None = None,
    ) -> None:
        """후보 소속 민원을 카테고리에 배정한다(벡터가 있으면 중심과의 유사도도 기록).

        파라미터:
            members: 후보 소속 민원
            category: 배정할 카테고리
            version: 체계 버전
            status: 배정 상태
            vectors: 민원 벡터
        """
        centroid = np.asarray(category.centroid, dtype=np.float32)
        await self._complaints.update_fields(
            [
                (
                    m.id,
                    {
                        "categoryId": category.category_id,
                        "group": category.group,
                        "assignStatus": status,
                        "taxonomyVersion": version,
                        "similarity": round(float(vectors[m.id] @ centroid), 4)
                        if vectors and m.id in vectors
                        else None,
                    },
                )
                for m in members
                # 그사이 검토 등으로 이미 배정·확정된 민원은 덮어쓰지 않는다
                if m.assign_status == "unassigned"
            ]
        )

    async def reviews(
        self, status: str | None, skip: int, limit: int
    ) -> tuple[list[tuple[ReviewDocument, ComplaintDocument | None]], int]:
        """검토 목록과 대상 민원, 전체 건수.

        파라미터:
            status: pending 또는 done(없으면 전체)
            skip: 건너뛸 개수
            limit: 최대 개수
        """
        if status not in (None, "pending", "done"):
            raise AppError("INVALID_STATUS", "status는 pending 또는 done입니다.", 422)
        # 위에서 검사한 문자열을 저장소가 받는 Literal 값으로 좁혀 넘긴다(None이면 전체)
        found, total = await self._reviews.list_page(
            "pending" if status == "pending" else "done" if status == "done" else None,
            skip,
            limit,
        )
        complaints = {
            c.id: c for c in await self._complaints.get_many([r.complaint_id for r in found])
        }
        return [(r, complaints.get(r.complaint_id)) for r in found], total

    async def review(self, review_id: str, request: ReviewDecisionRequest) -> ReviewDocument:
        """검토 결과를 저장한다. 카테고리를 주면 그 카테고리로 확정(confirmed)하고,
        비우면 지금 배정을 그대로 확인한다. 이미 끝난 검토면 409.

        파라미터:
            review_id: 검토 ID 문자열
            request: 확정 카테고리와 검토자
        """
        oid = parse_object_id(review_id, "REVIEW_NOT_FOUND", "검토를 찾을 수 없습니다.")
        review = await self._reviews.get(oid)
        if review is None:
            raise AppError("REVIEW_NOT_FOUND", "검토를 찾을 수 없습니다.", 404)
        if review.status != "pending":
            raise AppError("REVIEW_DONE", "이미 끝난 검토입니다.", 409)
        # 검토가 끝나면 어떤 경우든 검토 대기 표시는 내린다
        fields: dict[str, object] = {"needsReview": False}
        # 사람이 고른 카테고리는 현재 체계의 활성 카테고리여야 하고, 자동 배정(auto)과 구분해
        # confirmed로 남긴다(PRD 8.1 배정 상태의 "사람 확정")
        if request.category_id is not None:
            latest = await self._categories.latest_version()
            category = (
                await self._categories.get_in_version(request.category_id, latest)
                if latest
                else None
            )
            if category is None or category.status != "active":
                raise AppError("CATEGORY_NOT_FOUND", "카테고리가 현재 체계에 없습니다.", 422)
            fields.update(
                {
                    "categoryId": category.category_id,
                    "group": category.group,
                    "assignStatus": "confirmed",
                    "taxonomyVersion": latest,
                }
            )
        now = utc_now()
        # 검토 완료를 먼저 기록하고(pending일 때만 성공), 성공한 쪽만 민원을 고쳐 동시 저장을 막는다
        if not await self._reviews.complete(oid, request.category_id, request.reviewed_by, now):
            raise AppError("REVIEW_DONE", "이미 끝난 검토입니다.", 409)
        await self._complaints.update_fields([(review.complaint_id, fields)])
        # 저장된 완료 시각·검토자를 담아 돌려주려고 다시 읽는다
        updated = await self._reviews.get(oid)
        assert updated is not None
        return updated
