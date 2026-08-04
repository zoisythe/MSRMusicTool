from __future__ import annotations

import pytest

from msr_music_tool.catalog import Catalog
from msr_music_tool.models import AlbumSummary, SongSummary


@pytest.fixture
def sample_catalog() -> Catalog:
    albums = (
        AlbumSummary("a1", "新专辑", "https://cdn.test/cover-a1.jpg", ("MSR",), 0),
        AlbumSummary("a2", "旧专辑", "https://cdn.test/cover-a2.png", ("MSR",), 1),
        AlbumSummary("a3", "空专辑", "https://cdn.test/cover-a3.png", (), 2),
    )
    songs = (
        SongSummary("s1", "First Song", "a1", ("MSR",)),
        SongSummary("s2", "Same:Song?", "a1", ("MSR",)),
        SongSummary("s3", "Same:Song?", "a2", ("MSR",)),
        SongSummary("s4", "Another", "a2", ("MSR",)),
    )
    return Catalog(albums, songs)
