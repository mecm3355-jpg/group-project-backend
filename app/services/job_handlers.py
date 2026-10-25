"""작업 종류(processing_jobs.type)별 처리기 등록표.

업로드 때 만드는 "analyze"(추출·진단) 처리기는 파일 형식별 추출기(app/processors/)와 함께 추가한다.
등록되지 않은 종류의 작업은 워커가 가져가지 않고 queued로 남는다.
"""

from app.services.job_runner import JobHandler

HANDLERS: dict[str, JobHandler] = {}
