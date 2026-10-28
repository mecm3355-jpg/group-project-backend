from pathlib import Path

import numpy as np

from app.services.embedding import CachedEmbedder, Vectors, normalize


class CountingEmbedder:
    """본문 길이로 벡터를 만드는 가짜 임베더. 계산한 본문을 기록한다."""

    model_name = "fake-model"

    def __init__(self) -> None:
        """가짜 임베더를 만든다.

        파라미터: 없음
        """
        self.computed: list[str] = []

    def embed(self, texts: list[str]) -> Vectors:
        """본문마다 [길이, 1] 벡터를 정규화해 돌려준다.

        파라미터:
            texts: 본문 목록
        """
        self.computed.extend(texts)
        return normalize(np.asarray([[len(t), 1.0] for t in texts]))


def test_normalize_keeps_zero_rows() -> None:
    result = normalize(np.asarray([[3.0, 4.0], [0.0, 0.0]]))

    assert np.allclose(result, [[0.6, 0.8], [0.0, 0.0]])


def test_cached_embedder_reuses_saved_vectors(tmp_path: Path) -> None:
    inner = CountingEmbedder()
    embedder = CachedEmbedder(inner, tmp_path)

    first = embedder.embed(["가로등", "쓰레기 투기"])
    second = embedder.embed(["쓰레기 투기", "새 민원 본문", "가로등"])

    assert inner.computed == ["가로등", "쓰레기 투기", "새 민원 본문"]
    assert np.allclose(second[0], first[1])
    assert np.allclose(second[2], first[0])
    assert second.dtype == np.float32


def test_cache_is_separate_per_model(tmp_path: Path) -> None:
    inner = CountingEmbedder()
    CachedEmbedder(inner, tmp_path).embed(["가로등"])
    other = CountingEmbedder()
    other.model_name = "other-model"

    CachedEmbedder(other, tmp_path).embed(["가로등"])

    assert other.computed == ["가로등"]
