# SPDX-License-Identifier: MIT
import pytest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    mark = pytest.mark.image("container-distroless-base")
    for item in items:
        if Path(item.fspath).resolve().is_relative_to(_THIS_DIR):
            item.add_marker(mark)
