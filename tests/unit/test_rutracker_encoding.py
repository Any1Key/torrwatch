import pytest

from torrwatch.trackers.rutracker.parser import (
    RuTrackerParseError,
    decode_topic_page,
    parse_topic_page,
)


@pytest.mark.parametrize(
    "encoding,headers,meta",
    [
        ("cp1251", {"Content-Type": "text/html; charset=windows-1251"}, ""),
        ("cp1251", {}, '<meta charset="windows-1251">'),
        ("cp1251", {}, ""),
        ("utf-8", {"content-type": "text/html; charset=UTF-8"}, ""),
    ],
)
def test_topic_title_is_decoded(encoding, headers, meta):
    url = "https://rutracker.org/forum/viewtopic.php?t=123"
    html = f'<html><head>{meta}<meta property="og:title" content="Название сериала, серии 1–5"></head><body><a href="dl.php?t=123">download</a></body></html>'
    parsed = parse_topic_page(decode_topic_page(html.encode(encoding), headers), url, "123")
    assert parsed.title == "Название сериала, серии 1–5"


def test_invalid_declared_utf8_is_rejected():
    with pytest.raises(RuTrackerParseError):
        decode_topic_page("Название".encode("cp1251"), {"content-type": "text/html; charset=utf-8"})
