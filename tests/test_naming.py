from __future__ import annotations

from msr_music_tool.catalog import Catalog
from msr_music_tool.models import AlbumSummary, SongSummary
from msr_music_tool.naming import FileNamer, extension_from_remote, sanitize_component


def test_sanitize_component_uses_full_width_replacements() -> None:
    assert sanitize_component('  A<B>:C/D\\E|F?G*H".  ') == "A＜B＞：C／D＼E｜F？G＊H＂"


def test_duplicate_names_always_receive_album_and_cid_suffix(sample_catalog: Catalog) -> None:
    namer = FileNamer(sample_catalog)

    first = namer.filename(sample_catalog.song_by_cid["s2"], ".wav")
    second = namer.filename(sample_catalog.song_by_cid["s3"], ".wav")

    assert first == "塞壬唱片-MSR - Same：Song？ [新专辑-s2].wav"
    assert second == "塞壬唱片-MSR - Same：Song？ [旧专辑-s3].wav"


def test_cleaned_names_are_compared_case_insensitively() -> None:
    album = AlbumSummary("a", "Album", "https://cdn.test/a.png", (), 0)
    songs = (
        SongSummary("1", "Name?", "a", ()),
        SongSummary("2", "name？", "a", ()),
    )
    namer = FileNamer(Catalog((album,), songs))

    assert "[Album-1]" in namer.filename(songs[0], ".wav")
    assert "[Album-2]" in namer.filename(songs[1], ".wav")


def test_long_filename_preserves_prefix_cid_and_byte_limit() -> None:
    album = AlbumSummary("a", "Album", "https://cdn.test/a.png", (), 0)
    song = SongSummary("123456", "很长的歌曲名字" * 30, "a", ())
    namer = FileNamer(Catalog((album,), (song,)))

    filename = namer.filename(song, ".flac")

    assert filename.startswith("塞壬唱片-MSR - ")
    assert "~123456" in filename
    assert len(filename.encode("utf-8")) <= 240


def test_long_album_suffix_is_also_shortened_without_losing_uniqueness() -> None:
    album = AlbumSummary("a", "很长的专辑名字" * 30, "https://cdn.test/a.png", (), 0)
    songs = (
        SongSummary("123", "重名歌曲" * 30, "a", ()),
        SongSummary("456", "重名歌曲" * 30, "a", ()),
    )
    namer = FileNamer(Catalog((album,), songs))

    filenames = [namer.filename(song, ".wav") for song in songs]

    assert len(set(filenames)) == 2
    assert all(len(filename.encode("utf-8")) <= 240 for filename in filenames)
    assert "123" in filenames[0]
    assert "456" in filenames[1]


def test_remote_extension_prefers_url_then_content_type() -> None:
    assert extension_from_remote("https://cdn.test/song.WAV?token=x", "audio/mpeg") == ".wav"
    assert extension_from_remote("https://cdn.test/no-suffix", "image/jpeg") == ".jpg"
    assert extension_from_remote("https://cdn.test/no-suffix", None, default=".img") == ".img"
