from pathlib import Path

import pytest

from app.core.analysis_config import DEFAULT_PATH, load_analysis_config
from app.core.errors import AppError


def test_loads_repository_config() -> None:
    config = load_analysis_config(DEFAULT_PATH)

    assert config.assignment.threshold == 0.80
    assert config.discovery.candidate_min_size == 5
    assert config.spike.ratio == 1.5
    assert config.discovery.min_cohesion == 0.0


def test_missing_values_use_defaults(tmp_path: Path) -> None:
    path = tmp_path / "analysis.toml"
    path.write_text("[assignment]\nthreshold = 0.8\n", encoding="utf-8")

    config = load_analysis_config(path)

    assert config.assignment.threshold == 0.8
    assert config.assignment.ambiguity_margin == 0.05
    assert config.spike.baseline_months == 3


@pytest.mark.parametrize(
    "content",
    [
        "[assignment]\nthreshhold = 0.8\n",  # 오타 난 키
        "[assignment]\nthreshold = 1.5\n",  # 범위 밖
        "[assignment\nthreshold = 0.8\n",  # TOML 형식 오류
    ],
)
def test_rejects_invalid_config(tmp_path: Path, content: str) -> None:
    path = tmp_path / "analysis.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(AppError) as error:
        load_analysis_config(path)

    assert error.value.code == "INVALID_ANALYSIS_CONFIG"


def test_missing_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        load_analysis_config(tmp_path / "none.toml")
