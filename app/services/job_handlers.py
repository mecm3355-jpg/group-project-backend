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

# 새 작업 종류를 추가할 때는 처리기를 만들고 여기에 한 줄 등록한다. 워커는 이 키 목록으로만
# 큐를 조회하므로, API가 먼저 배포돼도 워커가 모르는 작업을 잘못 가져가 실패시키지 않는다
HANDLERS: dict[str, JobHandler] = {
    ANALYZE_JOB_TYPE: analyze,
    CLEAN_JOB_TYPE: clean,
    REPORT_JOB_TYPE: report,
    MERGE_JOB_TYPE: merge,
    COMPLAINT_ANALYSIS_JOB_TYPE: analyze_complaints,
}
