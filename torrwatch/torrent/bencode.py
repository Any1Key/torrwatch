"""Strict bencode decoder retaining source spans for exact infohashes."""

from __future__ import annotations

from dataclasses import dataclass


class BencodeDecodeError(ValueError):
    """The payload is not a canonical, bounded bencode value."""


@dataclass(frozen=True)
class BencodeNode:
    """Decoded bencode node and its exclusive source byte span."""

    value: BencodeValue
    start: int
    end: int


type BencodeValue = int | bytes | list[BencodeNode] | dict[bytes, BencodeNode]


class _Decoder:
    def __init__(self, payload: bytes, max_depth: int = 128) -> None:
        self._payload = payload
        self._position = 0
        self._max_depth = max_depth

    def decode(self) -> BencodeNode:
        result = self._parse_value(0)
        if self._position != len(self._payload):
            raise BencodeDecodeError("Trailing bytes after bencode value.")
        return result

    def _parse_value(self, depth: int) -> BencodeNode:
        if depth > self._max_depth:
            raise BencodeDecodeError("Bencode nesting limit exceeded.")
        if self._position >= len(self._payload):
            raise BencodeDecodeError("Unexpected end of bencode payload.")

        marker = self._payload[self._position]
        if marker == ord("i"):
            return self._parse_integer()
        if marker == ord("l"):
            return self._parse_list(depth)
        if marker == ord("d"):
            return self._parse_dictionary(depth)
        if ord("0") <= marker <= ord("9"):
            return self._parse_bytes()
        raise BencodeDecodeError("Invalid bencode value marker.")

    def _parse_integer(self) -> BencodeNode:
        start = self._position
        self._position += 1
        end_marker = self._payload.find(b"e", self._position)
        if end_marker == -1:
            raise BencodeDecodeError("Unterminated bencode integer.")
        encoded = self._payload[self._position : end_marker]
        if not encoded:
            raise BencodeDecodeError("Empty bencode integer.")
        if encoded == b"-0" or (encoded.startswith(b"0") and len(encoded) > 1):
            raise BencodeDecodeError("Non-canonical bencode integer.")
        digits = encoded[1:] if encoded.startswith(b"-") else encoded
        if not digits or not digits.isdigit():
            raise BencodeDecodeError("Invalid bencode integer.")
        self._position = end_marker + 1
        return BencodeNode(value=int(encoded), start=start, end=self._position)

    def _parse_bytes(self) -> BencodeNode:
        start = self._position
        colon = self._payload.find(b":", self._position)
        if colon == -1:
            raise BencodeDecodeError("Unterminated byte-string length.")
        encoded_length = self._payload[self._position : colon]
        if not encoded_length or not encoded_length.isdigit():
            raise BencodeDecodeError("Invalid byte-string length.")
        if encoded_length.startswith(b"0") and len(encoded_length) > 1:
            raise BencodeDecodeError("Non-canonical byte-string length.")
        length = int(encoded_length)
        value_start = colon + 1
        value_end = value_start + length
        if value_end > len(self._payload):
            raise BencodeDecodeError("Truncated bencode byte string.")
        self._position = value_end
        return BencodeNode(value=self._payload[value_start:value_end], start=start, end=value_end)

    def _parse_list(self, depth: int) -> BencodeNode:
        start = self._position
        self._position += 1
        values: list[BencodeNode] = []
        while True:
            if self._position >= len(self._payload):
                raise BencodeDecodeError("Unterminated bencode list.")
            if self._payload[self._position] == ord("e"):
                self._position += 1
                return BencodeNode(value=values, start=start, end=self._position)
            values.append(self._parse_value(depth + 1))

    def _parse_dictionary(self, depth: int) -> BencodeNode:
        start = self._position
        self._position += 1
        values: dict[bytes, BencodeNode] = {}
        previous_key: bytes | None = None
        while True:
            if self._position >= len(self._payload):
                raise BencodeDecodeError("Unterminated bencode dictionary.")
            if self._payload[self._position] == ord("e"):
                self._position += 1
                return BencodeNode(value=values, start=start, end=self._position)
            key_node = self._parse_bytes()
            if not isinstance(
                key_node.value, bytes
            ):  # Defensive: _parse_bytes always returns bytes.
                raise BencodeDecodeError("Dictionary key must be a byte string.")
            key = key_node.value
            if previous_key is not None and key <= previous_key:
                raise BencodeDecodeError("Dictionary keys must be strictly sorted.")
            previous_key = key
            values[key] = self._parse_value(depth + 1)


def decode_bencode(payload: bytes) -> BencodeNode:
    """Decode one canonical bencode value and retain each node's raw span."""

    return _Decoder(payload).decode()
