from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable

from .models import AlbumMatch, AlbumSummary, SelectionState, SongSummary


class Catalog:
    def __init__(
        self,
        albums: Iterable[AlbumSummary],
        songs: Iterable[SongSummary],
    ) -> None:
        self.albums = tuple(albums)
        self.songs = tuple(songs)
        self.album_by_cid = {album.cid: album for album in self.albums}
        self.song_by_cid = {song.cid: song for song in self.songs}
        grouped: defaultdict[str, list[SongSummary]] = defaultdict(list)
        for song in self.songs:
            grouped[song.album_cid].append(song)
        self.songs_by_album = {cid: tuple(items) for cid, items in grouped.items()}


class SelectionModel:
    def __init__(self, catalog: Catalog, selected: Iterable[str] = ()) -> None:
        self.catalog = catalog
        self.selected = {cid for cid in selected if cid in catalog.song_by_cid}

    def state_for_album(self, album_cid: str) -> SelectionState:
        songs = self.catalog.songs_by_album.get(album_cid, ())
        if not songs:
            return SelectionState.NONE
        selected_count = sum(song.cid in self.selected for song in songs)
        if selected_count == 0:
            return SelectionState.NONE
        if selected_count == len(songs):
            return SelectionState.FULL
        return SelectionState.PARTIAL

    def toggle_album(self, album_cid: str) -> None:
        song_ids = {song.cid for song in self.catalog.songs_by_album.get(album_cid, ())}
        if song_ids and song_ids <= self.selected:
            self.selected.difference_update(song_ids)
        else:
            self.selected.update(song_ids)

    def toggle_song(self, song_cid: str) -> None:
        if song_cid not in self.catalog.song_by_cid:
            return
        if song_cid in self.selected:
            self.selected.remove(song_cid)
        else:
            self.selected.add(song_cid)

    def album_matches(self, query: str = "") -> tuple[AlbumMatch, ...]:
        needle = query.strip().casefold()
        matches: list[AlbumMatch] = []
        for album in self.catalog.albums:
            songs = self.catalog.songs_by_album.get(album.cid, ())
            matched_songs = tuple(song for song in songs if needle in song.name.casefold())
            if needle and needle not in album.name.casefold() and not matched_songs:
                continue
            matches.append(
                AlbumMatch(
                    album=album,
                    state=self.state_for_album(album.cid),
                    matched_songs=matched_songs if needle else (),
                )
            )
        matches.sort(key=lambda item: (-item.state.value, item.album.release_rank))
        return tuple(matches)

    def selected_count_by_album(self) -> Counter[str]:
        return Counter(
            self.catalog.song_by_cid[cid].album_cid
            for cid in self.selected
            if cid in self.catalog.song_by_cid
        )

    def clear_completed(self, song_cids: Iterable[str]) -> None:
        self.selected.difference_update(song_cids)
