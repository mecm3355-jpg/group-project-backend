"""작업 종류(processing_jobs.type)별 처리기 등록표.

등록되지 않은 종류의 작업은 워커가 가져가지 않고 queued로 남는다.
"""

from app.schemas.complaint import COMPLAINT_ANALYSIS_JOB_TYPE
from app.schemas.job import ANALYZE_JOB_TYPE, CLEAN_JOB_TYPE, MERGE_JOB_TYPE, REPORT_JOB_TYPE
from app.services.analyze_handler import analyze
from app.services.clean_handler import clean
from app.services.complaint_handler import analyze_complaints
from app.services.job_runner import JobHandler
from app.services.merge_handler import merge
from app.services.report_handler import report

HANDLERS: dict[str, JobHandler] = {
    ANALYZE_JOB_TYPE: analyze,
    CLEAN_JOB_TYPE: clean,
    REPORT_JOB_TYPE: report,
    MERGE_JOB_TYPE: merge,
    COMPLAINT_ANALYSIS_JOB_TYPE: analyze_complaints,
}
