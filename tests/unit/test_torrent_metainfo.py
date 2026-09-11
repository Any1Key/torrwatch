from __future__ import annotations

from pathlib import Path

import pytest

from torrwatch.torrent import TorrentValidationError, parse_metainfo

FIXTURES = Path(__file__).parents[1] / "fixtures" / "torrents"


def _fixture(name: str) -> bytes:
    # Text fixtures are deliberately newline-terminated for source control; the
    # metainfo payload itself is the bytes preceding that fixture newline.
    return (FIXTURES / name).read_bytes().removesuffix(b"\n")


def test_v1_metainfo_has_known_exact_hashes() -> None:
    metadata = parse_metainfo(_fixture("valid-v1.torrent"))

    assert metadata.infohash_v1 == "ab843f6ded4224806b0880343b31cc4846a07788"
    assert metadata.infohash_v2 is None
    assert (
        metadata.torrent_sha256
        == "6d6f440c97f8326221afddba33bad153f6d5f0f63e819e5315f10b8d8529335b"
    )
    assert (metadata.name, metadata.total_size, metadata.file_count) == ("test", 4, 1)


def test_v2_metainfo_has_known_exact_hashes() -> None:
    metadata = parse_metainfo(_fixture("valid-v2.torrent"))

    assert metadata.infohash_v1 is None
    assert (
        metadata.infohash_v2 == "0db7b255491e5f7fff7fb98cb31916d94c0dd1c80270d2c2eceef79b191e8e23"
    )
    assert (
        metadata.torrent_sha256
        == "32845e8ee83f39c1f43ef2f37e425a57fad61559a37d21d46ea60331f7dd3061"
    )
    assert (metadata.name, metadata.total_size, metadata.file_count) == ("test", 4, 1)


@pytest.mark.parametrize(
    "payload",
    [
        b"d4:infoi1ee",  # info is not a dictionary
        b"d4:infod4:name4:testee",  # missing piece length and version
        b"d4:infod6:lengthi4e4:name4:test12:piece lengthi4e6:pieces19:abcdefghijklqrstee",
        b"d4:infod6:lengthi4e4:name4:test12:piece lengthi4e6:pieces20:abcdefghijklmnopqrsteeX",
        b"d4:infod6:lengthi4e4:name4:test12:piece lengthi4e6:pieces40:abcdefghijklmnopqrstabcdefghijklmnopqrstee",
        b"d4:infod4:name4:test9:file treed4:testd0:d6:lengthi4eeee12:meta versioni2e12:piece lengthi4eee",
        b"d4:infod6:lengthi4e4:name4:test12:piece lengthi4e6:pieces20:abcdefghijklmnopqrs",
    ],
)
def test_malformed_or_incomplete_metainfo_is_rejected(payload: bytes) -> None:
    with pytest.raises(TorrentValidationError):
        parse_metainfo(payload)


def test_noncanonical_dictionary_order_is_rejected() -> None:
    payload = b"d4:infod4:name4:test6:lengthi4e12:piece lengthi4e6:pieces20:abcdefghijklmnopqrsteee"

    with pytest.raises(TorrentValidationError):
        parse_metainfo(payload)


def test_v1_multi_file_paths_must_be_safe() -> None:
    payload = (
        b"d4:infod5:filesld6:lengthi4e4:pathl2:..4:testeee4:name4:test12:piece lengthi4e"
        b"6:pieces20:abcdefghijklmnopqrstee"
    )

    with pytest.raises(TorrentValidationError, match="unsafe path"):
        parse_metainfo(payload)


def test_v2_piece_length_must_follow_bep52_constraints() -> None:
    payload = (
        b"d4:infod9:file treed4:testd0:d6:lengthi4eeee12:meta versioni2e4:name4:test"
        b"12:piece lengthi4eee"
    )

    with pytest.raises(TorrentValidationError, match="v2 piece length"):
        parse_metainfo(payload)
