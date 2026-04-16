# SPDX-License-Identifier: MIT
"""Syrupy snapshot extension for plain-text snapshots.

Stores snapshots as UTF-8 ``.txt`` files — human-readable and
git-diffable.  Use in tests via::

    from utils.snapshot import TextSnapshotExtension

    def test_something(snapshot):
        assert data == snapshot(extension_class=TextSnapshotExtension)
"""

from __future__ import annotations

from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode


class TextSnapshotExtension(SingleFileSnapshotExtension):
    _write_mode = WriteMode.TEXT
    _text_encoding = "utf-8"
    file_extension = "txt"

    @classmethod
    def get_supported_dataclass(cls):  # type: ignore[override]
        return str
