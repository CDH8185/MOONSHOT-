import pytest
import requests

from cof_bot.news.feeds import MAX_FEED_BYTES, FeedError, NewsCollector, parse_feed

NOW = 1_790_000_000.0  # 2026-09-21T14:13:20Z

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Ondo surges &amp; &lt;b&gt;rallies&lt;/b&gt;</title><link>https://ex.com/a</link><guid>a1</guid>
<pubDate>Mon, 21 Sep 2026 13:00:00 GMT</pubDate><description>&lt;p&gt;body&lt;/p&gt;</description></item>
<item><title>Old story</title><link>https://ex.com/b</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title></title><link>https://ex.com/c</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom headline</title><link href="https://ex.com/atom1"/><id>tag:1</id><updated>2026-09-21T13:30:00Z</updated></entry>
</feed>"""


def test_parse_rss_cleans_and_dates():
    items = parse_feed(RSS, "ex", NOW)
    assert [h.title for h in items] == ["Ondo surges & rallies", "Old story"]
    assert items[0].published_at == NOW - 4400  # 13:00:00 vs 14:13:20
    assert items[0].summary == "body"
    assert parse_feed(RSS, "ex", NOW)[0].id == items[0].id  # stable id


def test_parse_atom():
    (h,) = parse_feed(ATOM, "ex", NOW)
    assert h.title == "Atom headline" and h.link == "https://ex.com/atom1"
    assert h.published_at == NOW - 2600


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel><item><title>&lol;</title></item></channel></rss>',
        b"<rss><channel><item><title>unclosed",
        b"x" * (MAX_FEED_BYTES + 1),
    ],
)
def test_unsafe_or_bad_xml_rejected(body):
    with pytest.raises(FeedError):
        parse_feed(body, "ex", NOW)


class Resp:
    def __init__(self, status=200, body=b"", headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i : i + n]

    def close(self):
        pass


class Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append((url, dict(headers or {})))
        r = self.routes.get(url)
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r or Resp(404)


def collector(routes, clock=lambda: NOW, **kw):
    return NewsCollector({"ex": "https://ex.com/feed"}, session=Session(routes), clock=clock, **kw)


def test_poll_filters_old_dedupes_and_uses_conditional_get():
    routes = {
        "https://ex.com/robots.txt": Resp(404),
        "https://ex.com/feed": [Resp(200, RSS, {"ETag": '"v1"'}), Resp(304)],
    }
    c = collector(routes)
    r = c.poll()
    assert [h.title for h in r.new] == ["Ondo surges & rallies"]  # old story dropped by age
    r2 = c.poll(force=True)
    assert r2.not_modified == ["ex"] and r2.new == []
    assert c.session.calls[-1][1]["If-None-Match"] == '"v1"'


def test_poll_respects_interval():
    c = collector({"https://ex.com/robots.txt": Resp(404), "https://ex.com/feed": Resp(200, RSS)})
    c.poll()
    assert c.poll().skipped == ["ex"]


def test_failure_backs_off_and_isolates():
    t = [NOW]
    c = collector(
        {"https://ex.com/robots.txt": Resp(404), "https://ex.com/feed": [Resp(503), requests.ConnectionError("x"), Resp(200, RSS)]},
        clock=lambda: t[0],
        poll_interval_s=100,
    )
    assert "HTTP 503" in c.poll().errors["ex"]
    assert c.feeds["ex"].next_poll_at == NOW + 200
    t[0] += 200
    assert "ex" in c.poll().errors
    assert c.feeds["ex"].next_poll_at == t[0] + 400
    t[0] += 400
    assert c.poll().new and c.feeds["ex"].failures == 0


def test_robots_disallow_skips_feed():
    c = collector({"https://ex.com/robots.txt": Resp(200, text="User-agent: *\nDisallow: /feed\n"), "https://ex.com/feed": Resp(200, RSS)})
    r = c.poll()
    assert "robots" in r.errors["ex"] and not r.new
    assert all("feed" not in url or "robots" in url for url, _ in c.session.calls)


def test_oversized_stream_rejected():
    c = collector({"https://ex.com/robots.txt": Resp(404), "https://ex.com/feed": Resp(200, b"x" * (MAX_FEED_BYTES + 10))})
    assert "larger" in c.poll().errors["ex"]
