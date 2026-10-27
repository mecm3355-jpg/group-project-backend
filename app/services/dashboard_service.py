"""전체 통계(GET /api/v1/dashboard/summary). 기준 컬렉션 3개를 그때그때 집계하고 저장하지 않는다."""

from typing import get_args

from app.repositories.files import FileRepository
from app.repositories.jobs import JobRepository
from app.repositories.results import ResultRepository
from app.schemas.dashboard_api import DashboardSummary, FileSummary, JobSummary, QualitySummary
from app.schemas.file import FileCategory, FileStatus
from app.schemas.job import JobStatus
from app.schemas.result import QualityResult, Severity
from app.services.profiling import SCORE_BASIS


def with_zeros(counts: dict[str, int], keys: tuple[str, ...]) -> dict[str, int]:
    """정해진 값 목록을 0으로 채운 뒤 집계를 덮어쓴다. 목록 밖의 값도 그대로 남긴다.

    파라미터:
        counts: 값별 개수
        keys: 항상 담을 값 목록(Literal 타입의 값들)
    """
    # 0건인 상태·분류도 항상 담아 화면이 키 유무를 따지지 않고 차트를 그리게 한다.
    # 목록 밖 값(예전 데이터의 옛 상태값)은 지우지 않아 집계 합계가 total과 맞는다
    return {**dict.fromkeys(keys, 0), **counts}


def summarize_quality(qualities: list[QualityResult]) -> QualitySummary:
    """파일별 최신 품질 진단 결과를 모아 평균 점수와 문제 유형·심각도별 개수를 낸다.

    평균 점수에는 지금 점수 기준(SCORE_BASIS)으로 계산한 점수만 넣는다. 예전 기준 점수는
    outdatedScoreFileCount로 세며, 그 파일을 다시 처리하면 새 기준 점수가 된다.

    파라미터:
        qualities: 파일마다 하나씩인 최신 품질 진단 결과
    """
    # 점수가 있는 것은 표 파일뿐이다(텍스트·문서는 문제 목록만 있고 score가 없다)
    scored = [q for q in qualities if q.score is not None]
    # 기준이 다른 점수를 섞어 평균 내면 숫자가 뜻을 잃으므로 지금 기준 점수만 평균에 넣는다
    scores = [q.score for q in scored if q.score is not None and q.score_basis == SCORE_BASIS]
    by_type: dict[str, int] = {}
    # 심각도는 high·medium·low가 늘 다 보이도록 0으로 채워 시작한다
    by_severity = dict.fromkeys(get_args(Severity), 0)
    # 문제 개수는 점수와 달리 기준 변경과 무관하므로 모든 파일에서 센다
    for quality in qualities:
        for issue in quality.issues:
            by_type[issue.type] = by_type.get(issue.type, 0) + 1
            by_severity[issue.severity] += 1
    return QualitySummary(
        analyzed_file_count=len(qualities),
        scored_file_count=len(scores),
        outdated_score_file_count=len(scored) - len(scores),
        # 점수 있는 파일이 없으면 0점이 아니라 None(아직 평균을 낼 수 없음)
        average_score=round(sum(scores) / len(scores), 1) if scores else None,
        # 많이 나온 문제 유형부터, 같으면 이름순으로 정렬해 매번 같은 순서로 보이게 한다
        issues_by_type=dict(sorted(by_type.items(), key=lambda item: (-item[1], item[0]))),
        issues_by_severity=by_severity,
    )


class DashboardService:
    """전체 통계 업무 로직."""

    def __init__(
        self, files: FileRepository, jobs: JobRepository, results: ResultRepository
    ) -> None:
        """서비스를 만든다.

        파라미터:
            files: files 저장소
            jobs: processing_jobs 저장소
            results: processing_results 저장소
        """
        self._files = files
        self._jobs = jobs
        self._results = results

    async def summary(self) -> DashboardSummary:
        """파일·작업·품질 진단 전체 통계를 만든다.

        파라미터: 없음
        """
        # 상태별 개수는 total과 by_status 두 곳에 쓰이므로 한 번만 집계한다
        file_status = await self._files.count_by("status")
        job_status = await self._jobs.count_by("status")
        # 집계 결과를 따로 저장하지 않고 매번 계산한다(설계 파일에 통계 컬렉션이 없다)
        return DashboardSummary(
            files=FileSummary(
                total=sum(file_status.values()),
                total_size_bytes=await self._files.total_size_bytes(),
                by_category=with_zeros(
                    await self._files.count_by("category"), get_args(FileCategory)
                ),
                by_status=with_zeros(file_status, get_args(FileStatus)),
            ),
            jobs=JobSummary(
                total=sum(job_status.values()),
                by_status=with_zeros(job_status, get_args(JobStatus)),
                # 작업 종류는 analyze·clean·report "등"으로 열려 있어(설계 파일)
                # 0 채우기를 하지 않는다
                by_type=await self._jobs.count_by("type"),
            ),
            # 파일마다 최신 분석 결과 하나만 본다(재처리 전 옛 버전이 평균에 섞이지 않게)
            quality=summarize_quality(await self._results.latest_qualities("analysis")),
        )
