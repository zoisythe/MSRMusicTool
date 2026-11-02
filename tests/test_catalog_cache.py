from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from msr_music_tool.catalog import Catalog
from msr_music_tool.catalog_cache import CachedCatalog, load_catalog_cache, save_catalog_cache


def test_cache_round_trip_preserves_catalog_and_timestamp(tmp_path: Path, sample_catalog: Catalog):
    path = tmp_path / "cache" / "catalog.json"
    snapshot = CachedCatalog(sample_catalog, datetime(2000, 1, 1, tzinfo=UTC))
    save_catalog_cache(path, snapshot)
    loaded = load_catalog_cache(path)
    assert loaded is not None
    assert loaded.fetched_at == snapshot.fetched_at
    assert loaded.catalog.albums == sample_catalog.albums
    assert loaded.catalog.songs == sample_catalog.songs
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("damage", ["missing", "json", "version", "date", "types", "duplicate"])
def test_unusable_cache_is_a_miss(tmp_path: Path, sample_catalog: Catalog, damage: str):
    path = tmp_path / "catalog.json"
    assert load_catalog_cache(path) is None
    if damage == "missing":
        return
    save_catalog_cache(path, CachedCatalog(sample_catalog, datetime.now(UTC)))
    raw = json.loads(path.read_text(encoding="utf-8"))
    if damage == "json":
        path.write_text("{broken", encoding="utf-8")
    else:
        if damage == "version":
            raw["schema_version"] = 2
        elif damage == "date":
            raw["fetched_at"] = "2000-01-01"
        elif damage == "types":
            raw["albums"][0]["artists"] = "artist"
        else:
            raw["songs"].append(raw["songs"][0])
        path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_catalog_cache(path) is None


def test_failed_atomic_replace_preserves_old_cache(
    tmp_path: Path,
    sample_catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "catalog.json"
    save_catalog_cache(path, CachedCatalog(sample_catalog, datetime(2000, 1, 1, tzinfo=UTC)))
    previous = path.read_bytes()

    def fail_replace(*_args):
        raise PermissionError("locked")

    monkeypatch.setattr("msr_music_tool.catalog_cache.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        save_catalog_cache(path, CachedCatalog(sample_catalog, datetime.now(UTC)))
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))
