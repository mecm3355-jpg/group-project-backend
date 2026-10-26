"""민원 임베딩 산출물(PRD 7.4: 개별 민원 벡터는 문서에 넣지 않고 산출물 파일로 보관).

작업마다 벡터 행렬(.npy)과 그 순서의 민원 _id 목록(.json)을 파일 산출물 폴더에 둔다.
후보 승인 때 소속 민원 벡터로 카테고리 중심을 계산하고, 소급 배정에 쓴다.
"""

import json
from collections.abc import Sequence

import numpy as np
from bson import ObjectId

from app.services.artifacts import results_dir_key
from app.services.embedding import Vectors
from app.services.storage import LocalStorage


def vectors_key(file_id: ObjectId, job_id: ObjectId) -> str:
    """작업 하나의 민원 벡터 파일 위치.

    파라미터:
        file_id: 원본 파일 _id
        job_id: 민원 분석 작업 _id
    """
    return f"{results_dir_key(file_id)}/complaints/{job_id}/embeddings.npy"


def vector_ids_key(file_id: ObjectId, job_id: ObjectId) -> str:
    """벡터 행 순서의 민원 _id 목록 위치.

    파라미터:
        file_id: 원본 파일 _id
        job_id: 민원 분석 작업 _id
    """
    return f"{results_dir_key(file_id)}/complaints/{job_id}/embedding-ids.json"


def save_vectors(
    storage: LocalStorage,
    file_id: ObjectId,
    job_id: ObjectId,
    complaint_ids: Sequence[ObjectId],
    vectors: Vectors,
) -> str:
    """벡터와 민원 _id 목록을 저장하고 벡터 파일 key를 돌려준다.

    파라미터:
        storage: 저장소
        file_id: 원본 파일 _id
        job_id: 작업 _id
        complaint_ids: vectors 행 순서의 민원 _id
        vectors: 정규화된 임베딩
    """
    key = vectors_key(file_id, job_id)
    path = storage.path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.asarray(vectors, dtype=np.float32))
    storage.write_text(vector_ids_key(file_id, job_id), json.dumps([str(i) for i in complaint_ids]))
    return key


def load_vectors(
    storage: LocalStorage, sources: Sequence[tuple[ObjectId, ObjectId, ObjectId]]
) -> dict[ObjectId, Vectors]:
    """(민원 _id, 파일 _id, 작업 _id) 목록의 벡터를 읽는다. 산출물이 없는 민원은 빠진다.

    파라미터:
        storage: 저장소
        sources: 읽을 민원과 그 민원을 만든 파일·작업
    """
    by_job: dict[tuple[ObjectId, ObjectId], set[ObjectId]] = {}
    for complaint_id, file_id, job_id in sources:
        by_job.setdefault((file_id, job_id), set()).add(complaint_id)
    found: dict[ObjectId, Vectors] = {}
    for (file_id, job_id), wanted in by_job.items():
        path = storage.path(vectors_key(file_id, job_id))
        ids_path = storage.path(vector_ids_key(file_id, job_id))
        if not path.is_file() or not ids_path.is_file():
            continue
        matrix = np.load(path)
        ids = [ObjectId(i) for i in json.loads(ids_path.read_text(encoding="utf-8"))]
        for row, complaint_id in enumerate(ids):
            if complaint_id in wanted:
                found[complaint_id] = np.asarray(matrix[row], dtype=np.float32)
    return found
