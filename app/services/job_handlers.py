"""작업 종류(processing_jobs.type)별 처리기 등록표.

등록되지 않은 종류의 작업은 워커가 가져가지 않고 queued로 남는다.
"""

from app.schemas.job import ANALYZE_JOB_TYPE
from app.services.analyze_handler import analyze
from app.services.job_runner import JobHandler

HANDLERS: dict[str, JobHandler] = {
    ANALYZE_JOB_TYPE: analyze,
}
