"""Headline collection from public RSS and Atom feeds of crypto news outlets.

Only the syndication feeds each outlet publishes are read; article pages are
never fetched. Each request identifies the bot, uses conditional GET (ETag and
Last-Modified) so an unchanged feed costs one small response, respects the
host's robots.txt, and is size capped. Feeds are untrusted XML: any document
that declares a DOCTYPE or ENTITY is rejected before parsing.

Default feeds: each returned HTTP 200 with items from this project's build
environment on 2026-09-27. Coinbase's own blog feed returned 403 and is not
included.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
import urllib.robotparser
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger(__name__)

USER_AGENT = "cof-bot/0.2 (headline sentiment research; RSS reader)"
MAX_FEED_BYTES = 5 * 1024 * 1024

DEFAULT_FEEDS: dict[str, str] = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
    "theblock": "https://www.theblock.co/rss.xml",
    "bitcoinmagazine": "https://bitcoinmagazine.com/.rss/full/",
    "cryptoslate": "https://cryptoslate.com/feed/",
    "cryptopotato": "https://cryptopotato.com/feed/",
    "newsbtc": "https://www.newsbtc.com/feed/",
    "utoday": "https://u.today/rss",
    "blockworks": "https://blockworks.co/feed",
    "thedefiant": "https://thedefiant.io/api/feed",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_UNSAFE_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


@dataclass(frozen=True)
class Headline:
    id: str
    source: str
    title: str
    link: str
    published_at: float | None  # epoch seconds, UTC
    fetched_at: float
    summary: str = ""

    @property
    def event_time(self) -> float:
        return self.published_at if self.published_at is not None else self.fetched_at


class FeedError(Exception):
    pass


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(elem: ET.Element, *names: str) -> str | None:
    for child in elem:
        if _local(child.tag) in names:
            if child.text and child.text.strip():
                return child.text
            href = child.get("href")
            if href:
                return href
    return None


def _parse_date(text: str | None) -> float | None:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)  # RFC 822, RSS 2.0
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # RFC 3339, Atom
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_feed(content: bytes, source: str, fetched_at: float) -> list[Headline]:
    if len(content) > MAX_FEED_BYTES:
        raise FeedError(f"{source}: feed larger than {MAX_FEED_BYTES} bytes")
    if _UNSAFE_XML.search(content):
        raise FeedError(f"{source}: feed declares a DOCTYPE or ENTITY; refused")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise FeedError(f"{source}: malformed XML: {exc}") from exc

    items = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    headlines: list[Headline] = []
    for item in items:
        title = _clean(_child_text(item, "title"))
        if not title:
            continue
        link = (_child_text(item, "link") or "").strip()
        guid = (_child_text(item, "guid", "id") or link or title).strip()
        published = _parse_date(_child_text(item, "pubdate", "published", "updated", "date"))
        summary = _clean(_child_text(item, "description", "summary"))[:500]
        hid = hashlib.sha256(f"{source}|{guid}".encode()).hexdigest()[:24]
        headlines.append(Headline(hid, source, title, link, published, fetched_at, summary))
    return headlines


@dataclass
class _FeedState:
    url: str
    etag: str | None = None
    last_modified: str | None = None
    failures: int = 0
    next_poll_at: float = 0.0
    robots_allowed: bool | None = None
    last_error: str | None = None
    items_seen: int = 0


@dataclass
class PollResult:
    new: list[Headline] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    not_modified: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class NewsCollector:
    def __init__(
        self,
        feeds: dict[str, str] | None = None,
        *,
        poll_interval_s: float = 120.0,
        max_age_s: float = 6 * 3600,
        timeout_s: float = 15.0,
        max_backoff_s: float = 3600.0,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        check_robots: bool = True,
    ):
        self.feeds = {name: _FeedState(url) for name, url in (feeds or DEFAULT_FEEDS).items()}
        self.poll_interval_s = poll_interval_s
        self.max_age_s = max_age_s
        self.timeout_s = timeout_s
        self.max_backoff_s = max_backoff_s
        self.session = session or requests.Session()
        self.clock = clock
        self.check_robots = check_robots
        self._seen: OrderedDict[str, None] = OrderedDict()

    def _robots_ok(self, name: str, state: _FeedState) -> bool:
        if not self.check_robots:
            return True
        if state.robots_allowed is None:
            parts = urlsplit(state.url)
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            parser = urllib.robotparser.RobotFileParser()
            try:
                resp = self.session.get(robots_url, timeout=self.timeout_s, headers={"User-Agent": USER_AGENT})
                if resp.status_code >= 400:
                    state.robots_allowed = True  # no robots.txt means no restriction
                else:
                    parser.parse(resp.text.splitlines())
                    state.robots_allowed = parser.can_fetch(USER_AGENT, state.url)
            except requests.RequestException as exc:
                log.warning("%s: robots.txt unreachable (%s); will retry", name, exc)
                return False
            if not state.robots_allowed:
                log.warning("%s: robots.txt disallows %s; feed skipped", name, state.url)
        return bool(state.robots_allowed)

    def _fetch(self, name: str, state: _FeedState, now: float) -> list[Headline] | None:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = self.session.get(state.url, headers=headers, timeout=self.timeout_s, stream=True)
        try:
            if resp.status_code == 304:
                return None
            if resp.status_code >= 400:  # 429 and 5xx included; poll() backs off
                raise FeedError(f"HTTP {resp.status_code}")
            body = bytearray()
            for chunk in resp.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_FEED_BYTES:
                    raise FeedError(f"feed larger than {MAX_FEED_BYTES} bytes")
        finally:
            resp.close()
        state.etag = resp.headers.get("ETag")
        state.last_modified = resp.headers.get("Last-Modified")
        return parse_feed(bytes(body), name, now)

    def poll(self, force: bool = False) -> PollResult:
        """Poll every feed that is due. One failing feed never blocks the rest."""
        now = self.clock()
        result = PollResult()
        for name, state in self.feeds.items():
            if not force and now < state.next_poll_at:
                result.skipped.append(name)
                continue
            if not self._robots_ok(name, state):
                state.next_poll_at = now + self.max_backoff_s
                result.errors[name] = "robots.txt disallows or is unreachable"
                continue
            try:
                items = self._fetch(name, state, now)
            except (requests.RequestException, FeedError) as exc:
                state.failures += 1
                state.last_error = str(exc)
                backoff = min(self.max_backoff_s, self.poll_interval_s * (2 ** min(state.failures, 10)))
                state.next_poll_at = now + backoff
                result.errors[name] = str(exc)
                log.warning("%s: feed failed (%s); next try in %.0fs", name, exc, backoff)
                continue
            state.failures = 0
            state.last_error = None
            state.next_poll_at = now + self.poll_interval_s
            if items is None:
                result.not_modified.append(name)
                continue
            state.items_seen += len(items)
            result.new.extend(self._fresh(items, now))
        result.new.sort(key=lambda h: h.event_time)
        return result

    def _fresh(self, items: Iterable[Headline], now: float) -> list[Headline]:
        fresh = []
        for h in items:
            if h.id in self._seen:
                continue
            self._seen[h.id] = None
            if len(self._seen) > 20_000:
                self._seen.popitem(last=False)
            if now - h.event_time > self.max_age_s or h.event_time - now > 300:
                continue  # too old to be news, or dated implausibly in the future
            fresh.append(h)
        return fresh
