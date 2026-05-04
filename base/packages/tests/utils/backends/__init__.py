# SPDX-License-Identifier: MIT
"""Public surface of the backend package.

Re-exports so callers (``conftest.py``, the repoclosure tests) can
``from utils.backends import RepoBackend, build_backend`` without
caring which concrete backend is in use.
"""

from .base import RepoBackend, build_backend

__all__ = ["RepoBackend", "build_backend"]
