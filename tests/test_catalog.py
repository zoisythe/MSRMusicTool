from __future__ import annotations

from msr_music_tool.catalog import Catalog, SelectionModel
from msr_music_tool.models import SelectionState


def test_album_selection_states_and_sorting(sample_catalog: Catalog) -> None:
    selection = SelectionModel(sample_catalog)
    assert [item.album.cid for item in selection.album_matches()] == ["a1", "a2", "a3"]

    selection.toggle_song("s2")
    selection.toggle_album("a2")

    matches = selection.album_matches()
    assert [item.album.cid for item in matches] == ["a2", "a1", "a3"]
    assert [item.state for item in matches] == [
        SelectionState.FULL,
        SelectionState.PARTIAL,
        SelectionState.NONE,
    ]


def test_toggle_album_selects_all_then_clears_all(sample_catalog: Catalog) -> None:
    selection = SelectionModel(sample_catalog)

    selection.toggle_album("a1")
    assert selection.selected == {"s1", "s2"}
    selection.toggle_album("a1")
    assert not selection.selected


def test_search_matches_album_and_song_names_case_insensitively(
    sample_catalog: Catalog,
) -> None:
    selection = SelectionModel(sample_catalog, {"s4"})

    album_match = selection.album_matches("新专辑")
    song_match = selection.album_matches("FIRST song")

    assert [item.album.cid for item in album_match] == ["a1"]
    assert [item.album.cid for item in song_match] == ["a1"]
    assert [song.cid for song in song_match[0].matched_songs] == ["s1"]
    assert selection.selected == {"s4"}


def test_clear_completed_preserves_failures(sample_catalog: Catalog) -> None:
    selection = SelectionModel(sample_catalog, {"s1", "s2", "s3"})

    selection.clear_completed({"s1", "s3"})

    assert selection.selected == {"s2"}
