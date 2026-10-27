"""문장 임베딩 어댑터(PRD 6장). 임베딩은 항상 로컬에서 만든다(원문이 밖으로 나가지 않음).

- 기본 모델은 bge-m3(sentence-transformers). 무거운 패키지라 처음 쓸 때 불러온다
  (pip install -e ".[analysis]", 첫 실행 때 모델 약 2.3GB를 Hugging Face 캐시에 받는다).
- 결과 벡터는 길이 1로 정규화해 내적 = 코사인 유사도가 되게 한다.
- 모델을 바꾸면 카테고리 중심을 모두 다시 계산해야 하므로 모델 이름을 결과마다 기록한다.
"""

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.core.errors import AppError

Vectors = NDArray[np.float32]

# 한국어를 지원하는 다국어 문장 임베딩(PRD 6장 "bge-m3 등"). 바꾸면 카테고리 중심을 모두 다시
# 계산해야 하므로 교체는 분기 정리 때만 한다(PRD 6장 공통 인터페이스)
DEFAULT_MODEL = "BAAI/bge-m3"


class Embedder(Protocol):
    """본문 목록을 정규화된 벡터 행렬(건수 × 차원)로 바꾼다."""

    model_name: str

    def embed(self, texts: list[str]) -> Vectors:
        """본문마다 벡터 하나를 만든다.

        파라미터:
            texts: 마스킹된 본문 목록
        """
        ...


def normalize(vectors: NDArray[np.floating]) -> Vectors:
    """행마다 길이 1로 맞춘다. 길이가 0인 행은 그대로 둔다.

    파라미터:
        vectors: 건수 × 차원 행렬
    """
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # 0으로 나누면 NaN이 퍼지므로 영벡터는 1로 나눠(=그대로) 유사도 0이 되게 둔다
    norms[norms == 0] = 1.0
    return np.asarray(matrix / norms, dtype=np.float32)


class SentenceTransformerEmbedder:
    """sentence-transformers 모델(기본 bge-m3)로 로컬 임베딩을 만든다."""

    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 32) -> None:
        """모델을 불러온다. 패키지가 없으면 AppError(EMBEDDING_NOT_AVAILABLE).

        파라미터:
            model_name: Hugging Face 모델 이름
            batch_size: 한 번에 계산할 본문 수
        """
        # torch까지 딸린 무거운 선택 패키지(analysis extra)라 모듈 맨 위가 아니라 여기서 불러온다.
        # 그래야 임베딩을 쓰지 않는 API·테스트는 패키지 없이도 뜬다
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise AppError(
                "EMBEDDING_NOT_AVAILABLE",
                '임베딩 패키지가 없습니다. pip install -e ".[analysis]"로 설치하세요.',
                503,
            ) from exc
        self.model_name = model_name
        self._batch_size = batch_size
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: list[str]) -> Vectors:
        """본문 목록을 정규화된 벡터로 바꾼다.

        파라미터:
            texts: 마스킹된 본문 목록
        """
        # 빈 목록을 모델에 넘기면 차원을 알 수 없으므로 빈 행렬을 바로 돌려준다
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        # 워커 로그를 어지럽히지 않게 진행 막대는 끈다. 정규화는 아래에서 직접 해 모델 설정과
        # 상관없이 내적 = 코사인 유사도를 보장한다
        encoded = self._model.encode(
            texts, batch_size=self._batch_size, convert_to_numpy=True, show_progress_bar=False
        )
        return normalize(np.asarray(encoded, dtype=np.float32))


class CachedEmbedder:
    """같은 모델·같은 본문의 벡터를 파일에 저장해 두고 다시 계산하지 않는다."""

    def __init__(self, inner: Embedder, cache_dir: Path) -> None:
        """캐시를 붙인 임베더를 만든다.

        파라미터:
            inner: 실제로 계산할 임베더
            cache_dir: 벡터 파일(.npy)을 둘 폴더. 마스킹본에서 나온 벡터만 저장한다
        """
        self.model_name = inner.model_name
        self._inner = inner
        self._dir = cache_dir

    def _path(self, text: str) -> Path:
        """본문 하나의 캐시 파일 위치(모델 이름 + 본문 해시).

        파라미터:
            text: 본문
        """
        # 모델 이름을 해시에 섞어 모델을 바꾸면 다른 파일을 보게 한다(차원·값이 달라지므로).
        # 본문 대신 해시를 파일명으로 써 마스킹본이라도 파일명에 내용이 드러나지 않는다
        digest = hashlib.sha256(f"{self.model_name}\n{text}".encode()).hexdigest()
        # 한 폴더에 파일이 너무 많이 쌓이지 않도록 해시 앞 두 글자로 하위 폴더를 나눈다
        return self._dir / digest[:2] / f"{digest}.npy"

    def embed(self, texts: list[str]) -> Vectors:
        """캐시에 없는 본문만 계산하고, 입력 순서대로 벡터를 돌려준다.

        파라미터:
            texts: 마스킹된 본문 목록
        """
        # 1) 캐시에 있는 벡터는 읽고, 없는 본문의 위치만 모은다
        found: dict[int, Vectors] = {}
        missing: list[int] = []
        for index, text in enumerate(texts):
            path = self._path(text)
            if path.exists():
                found[index] = np.asarray(np.load(path), dtype=np.float32)
            else:
                missing.append(index)
        # 2) 빠진 것만 한 번에 묶어 계산해 저장한다. 재분석·시뮬레이션에서 같은 본문이 반복되므로
        #    bge-m3를 다시 돌리는 시간을 아낀다
        if missing:
            computed = self._inner.embed([texts[i] for i in missing])
            for row, index in enumerate(missing):
                path = self._path(texts[index])
                path.parent.mkdir(parents=True, exist_ok=True)
                np.save(path, computed[row])
                found[index] = computed[row]
        # 3) np.stack은 빈 목록을 받지 못하므로 따로 처리하고, 나머지는 입력 순서대로 쌓는다
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        return np.stack([found[i] for i in range(len(texts))]).astype(np.float32)


# 테스트용 대체 임베더. 설정돼 있으면 get_embedder가 실제 모델 대신 이것을 돌려준다
_override: Embedder | None = None


def set_embedder_override(embedder: Embedder | None) -> None:
    """get_embedder가 돌려줄 임베더를 바꾼다(테스트에서 가짜 임베더를 쓸 때). None이면 되돌린다.

    파라미터:
        embedder: 대신 쓸 임베더
    """
    global _override
    _override = embedder


@lru_cache(maxsize=2)
def _cached_embedder(model_name: str, cache_dir: Path) -> Embedder:
    """모델을 한 번만 불러 캐시를 붙인다(bge-m3는 불러오는 데 수 초, 메모리 2GB 남짓).

    파라미터:
        model_name: 임베딩 모델 이름
        cache_dir: 벡터 캐시 폴더
    """
    return CachedEmbedder(SentenceTransformerEmbedder(model_name), cache_dir)


def get_embedder(settings: Settings) -> Embedder:
    """설정의 임베딩 모델(EMBEDDING_MODEL)로 캐시 붙은 임베더를 돌려준다.

    파라미터:
        settings: 앱 설정
    """
    if _override is not None:
        return _override
    # 캐시 폴더를 따로 정하지 않으면 저장소 아래에 둔다. resolve()로 절대 경로를 만들어
    # 같은 폴더를 다르게 적어도 lru_cache 키가 같아지게 한다
    cache_dir = settings.embedding_cache_dir or settings.storage_root / "embedding-cache"
    return _cached_embedder(settings.embedding_model, cache_dir.resolve())
