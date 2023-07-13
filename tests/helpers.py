"""Helpers for lock_code_manager tests."""

from functools import lru_cache
from pathlib import Path
import traceback


@lru_cache
def load_fixture(path_from_fixtures_folder: str) -> str:
    """Load a fixture."""
    parent_path = Path(traceback.extract_stack()[-2].filename).parent
    return (parent_path / "fixtures" / path_from_fixtures_folder).read_text()
