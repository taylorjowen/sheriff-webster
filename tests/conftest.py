import pytest

from sheriff.config import Config
from sheriff.patterns import shared_word_data


@pytest.fixture
def cfg(tmp_path):
    return Config({
        "env": "test",
        "paths": {"live_db": str(tmp_path / "live.db"), "archive_dir": str(tmp_path / "archive"),
                  "test_dir": str(tmp_path / "runs")},
        "scoring": {"samples": 48},
    })


@pytest.fixture(scope="session")
def wd():
    return shared_word_data(Config().path("cache_dir"))
