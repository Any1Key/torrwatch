"""Validation and exact hashing of BitTorrent metainfo files."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1, sha256

from torrwatch.torrent.bencode import BencodeDecodeError, BencodeNode, decode_bencode


class TorrentValidationError(ValueError):
    """A torrent payload is malformed or unsupported by the torrent engine."""


@dataclass(frozen=True)
class TorrentMetadata:
    """Validated, non-secret metadata extracted from a metainfo document."""

    torrent_sha256: str
    infohash_v1: str | None
    infohash_v2: str | None
    name: str
    total_size: int
    file_count: int


def _as_dict(node: BencodeNode, field: str) -> dict[bytes, BencodeNode]:
    if not isinstance(node.value, dict):
        raise TorrentValidationError(f"{field} must be a dictionary.")
    return node.value


def _as_list(node: BencodeNode, field: str) -> list[BencodeNode]:
    if not isinstance(node.value, list):
        raise TorrentValidationError(f"{field} must be a list.")
    return node.value


def _as_int(node: BencodeNode, field: str, *, minimum: int = 0) -> int:
    if not isinstance(node.value, int) or node.value < minimum:
        raise TorrentValidationError(f"{field} must be an integer of at least {minimum}.")
    return node.value


def _as_text(node: BencodeNode, field: str) -> str:
    if not isinstance(node.value, bytes):
        raise TorrentValidationError(f"{field} must be a byte string.")
    try:
        value = node.value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TorrentValidationError(f"{field} must be UTF-8.") from exc
    if not value:
        raise TorrentValidationError(f"{field} must not be empty.")
    return value


def _field(values: dict[bytes, BencodeNode], key: bytes, field: str) -> BencodeNode:
    try:
        return values[key]
    except KeyError as exc:
        raise TorrentValidationError(f"Missing required {field}.") from exc


def _valid_path_component(value: str, field: str) -> None:
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise TorrentValidationError(f"{field} contains an unsafe path component.")


def _v1_files(info: dict[bytes, BencodeNode]) -> tuple[int, int]:
    has_length = b"length" in info
    has_files = b"files" in info
    if has_length == has_files:
        raise TorrentValidationError("v1 metainfo needs exactly one of length or files.")
    if has_length:
        return _as_int(_field(info, b"length", "length"), "length"), 1

    files = _as_list(_field(info, b"files", "files"), "files")
    if not files:
        raise TorrentValidationError("files must not be empty.")
    total_size = 0
    for index, entry_node in enumerate(files):
        entry = _as_dict(entry_node, f"files[{index}]")
        length = _as_int(_field(entry, b"length", f"files[{index}].length"), "file length")
        path = _as_list(_field(entry, b"path", f"files[{index}].path"), "file path")
        if not path:
            raise TorrentValidationError("file path must not be empty.")
        for component in path:
            _valid_path_component(_as_text(component, "file path component"), "file path component")
        total_size += length
    return total_size, len(files)


def _v2_files(tree_node: BencodeNode) -> tuple[int, int]:
    tree = _as_dict(tree_node, "file tree")
    if not tree:
        raise TorrentValidationError("file tree must not be empty.")

    def walk(branch: dict[bytes, BencodeNode], prefix: str) -> tuple[int, int]:
        total_size = 0
        file_count = 0
        leaf = branch.get(b"")
        if leaf is not None:
            if len(branch) != 1:
                raise TorrentValidationError("v2 file leaf cannot have child paths.")
            leaf_data = _as_dict(leaf, "v2 file leaf")
            return _as_int(_field(leaf_data, b"length", "v2 file length"), "v2 file length"), 1
        for raw_name, child in branch.items():
            name = _as_text(BencodeNode(raw_name, 0, 0), "v2 file path component")
            _valid_path_component(name, "v2 file path component")
            child_total, child_count = walk(
                _as_dict(child, f"file tree {prefix}{name}"), f"{prefix}{name}/"
            )
            total_size += child_total
            file_count += child_count
        if file_count == 0:
            raise TorrentValidationError("v2 file tree directory cannot be empty.")
        return total_size, file_count

    return walk(tree, "")


def parse_metainfo(payload: bytes) -> TorrentMetadata:
    """Strictly validate a metainfo file and calculate exact v1/v2 hashes.

    The v1 and v2 hashes intentionally use the original raw span of ``info``.
    Re-encoding decoded values would change the meaning of malformed/noncanonical
    torrents and is never used for an infohash.
    """

    try:
        root = decode_bencode(payload)
    except BencodeDecodeError as exc:
        raise TorrentValidationError("Invalid bencode metainfo.") from exc
    values = _as_dict(root, "metainfo")
    info_node = _field(values, b"info", "info dictionary")
    info = _as_dict(info_node, "info dictionary")

    name_node = info.get(b"name.utf-8") or info.get(b"name")
    if name_node is None:
        raise TorrentValidationError("Missing required name.")
    name = _as_text(name_node, "name")
    _valid_path_component(name, "name")

    piece_length = _as_int(_field(info, b"piece length", "piece length"), "piece length", minimum=1)
    is_v2 = b"meta version" in info
    if is_v2 and _as_int(info[b"meta version"], "meta version") != 2:
        raise TorrentValidationError("Unsupported meta version.")
    if is_v2 and (piece_length < 16_384 or piece_length & (piece_length - 1)):
        raise TorrentValidationError("v2 piece length must be a power of two of at least 16 KiB.")

    has_v1 = b"pieces" in info
    has_v2 = is_v2
    if not has_v1 and not has_v2:
        raise TorrentValidationError("Metainfo is neither v1 nor v2.")

    v1_size: int | None = None
    v1_count: int | None = None
    if has_v1:
        pieces = _field(info, b"pieces", "pieces")
        if not isinstance(pieces.value, bytes) or len(pieces.value) % 20 != 0:
            raise TorrentValidationError("pieces must contain complete SHA-1 hashes.")
        v1_size, v1_count = _v1_files(info)
        expected_pieces = (v1_size + piece_length - 1) // piece_length
        if len(pieces.value) // 20 != expected_pieces:
            raise TorrentValidationError("pieces count does not match content length.")

    v2_size: int | None = None
    v2_count: int | None = None
    if has_v2:
        v2_size, v2_count = _v2_files(_field(info, b"file tree", "file tree"))

    if v1_size is not None and v2_size is not None and (v1_size, v1_count) != (v2_size, v2_count):
        raise TorrentValidationError("Hybrid v1/v2 file layouts disagree.")

    total_size, file_count = (v1_size, v1_count) if v1_size is not None else (v2_size, v2_count)
    if total_size is None or file_count is None:  # Defensive narrowing for type checkers.
        raise TorrentValidationError("Metainfo contains no files.")
    raw_info = payload[info_node.start : info_node.end]
    return TorrentMetadata(
        torrent_sha256=sha256(payload).hexdigest(),
        infohash_v1=sha1(raw_info).hexdigest() if has_v1 else None,
        infohash_v2=sha256(raw_info).hexdigest() if has_v2 else None,
        name=name,
        total_size=total_size,
        file_count=file_count,
    )
