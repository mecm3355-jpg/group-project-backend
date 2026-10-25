"""저장소 key 규칙. 원본 외의 파일(작업 파라미터, 산출물)은 파일·작업 ID별 폴더에 둔다."""

from bson import ObjectId


def job_params_key(job_id: ObjectId) -> str:
    """작업 파라미터 파일 위치. processing_jobs에 파라미터 필드가 없어 저장소로 넘긴다(PRD 7.7).

    파라미터:
        job_id: 작업 _id
    """
    return f"jobs/{job_id}/params.json"


def job_dir_key(job_id: ObjectId) -> str:
    """작업 하나의 임시 폴더 위치(작업이 끝나면 지운다).

    파라미터:
        job_id: 작업 _id
    """
    return f"jobs/{job_id}"


def results_dir_key(file_id: ObjectId) -> str:
    """파일 하나의 산출물 폴더 위치(파일을 지울 때 함께 지운다).

    파라미터:
        file_id: 파일 _id
    """
    return f"results/{file_id}"


def cleaned_data_key(file_id: ObjectId, version: int) -> str:
    """정제 결과 데이터(CSV) 위치.

    파라미터:
        file_id: 파일 _id
        version: 정제 결과 버전
    """
    return f"{results_dir_key(file_id)}/cleaning/v{version}.csv"
