"""Site crawler: robots-aware, same-domain, depth-limited.

Client onboarding is: paste a URL, click go. This is that.
"""

from __future__ import annotations

import asyncio
import urllib.robotparser as robotparser
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import trafilatura
from selectolax.parser import HTMLParser

from app.logging import get_logger

log = get_logger("crawler")

_SKIP_SUFFIXES = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".zip",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".xml",
    ".woff",
    ".woff2",
)


@dataclass
class Page:
    url: str
    title: str
    text: str


def _normalise(url: str) -> str:
    url, _ = urldefrag(url)
    return url.rstrip("/") or url


async def _robots(client: httpx.AsyncClient, root: str) -> robotparser.RobotFileParser:
    rp = robotparser.RobotFileParser()
    rp.set_url(urljoin(root, "/robots.txt"))
    try:
        resp = await client.get(urljoin(root, "/robots.txt"), timeout=10)
        rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    except Exception:
        rp.parse([])
    return rp


def _links(html: str, base: str, host: str) -> list[str]:
    out: list[str] = []
    for node in HTMLParser(html).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = _normalise(urljoin(base, href))
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or parsed.netloc != host:
            continue
        if absolute.lower().endswith(_SKIP_SUFFIXES):
            continue
        out.append(absolute)
    return out


async def crawl(
    start_url: str,
    *,
    max_pages: int = 40,
    max_depth: int = 3,
    concurrency: int = 4,
    user_agent: str = "FirstVoidBot/1.0 (+https://firstvoid.com)",
) -> list[Page]:
    start_url = _normalise(start_url)
    host = urlparse(start_url).netloc
    seen: set[str] = {start_url}
    pages: list[Page] = []
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        follow_redirects=True, headers={"User-Agent": user_agent}, timeout=20
    ) as client:
        rp = await _robots(client, start_url)
        frontier: list[tuple[str, int]] = [(start_url, 0)]

        while frontier and len(pages) < max_pages:
            batch, frontier = frontier[:concurrency], frontier[concurrency:]

            async def fetch(item: tuple[str, int]):
                url, depth = item
                if not rp.can_fetch(user_agent, url):
                    log.info("robots_disallow", url=url)
                    return None
                async with semaphore:
                    try:
                        resp = await client.get(url)
                    except Exception as exc:
                        log.warning("fetch_failed", url=url, error=str(exc))
                        return None
                if resp.status_code != 200 or "text/html" not in resp.headers.get(
                    "content-type", ""
                ):
                    return None
                return url, depth, resp.text

            for result in await asyncio.gather(*(fetch(i) for i in batch)):
                if not result:
                    continue
                url, depth, html = result

                extracted = trafilatura.extract(
                    html, include_comments=False, include_tables=True, favor_precision=True
                )
                if extracted and len(extracted.strip()) > 120:
                    tnode = HTMLParser(html).css_first("title")
                    pages.append(
                        Page(
                            url=url,
                            title=(tnode.text().strip() if tnode else "")[:300],
                            text=extracted.strip(),
                        )
                    )

                if depth < max_depth:
                    for link in _links(html, url, host):
                        if link not in seen and len(seen) < max_pages * 4:
                            seen.add(link)
                            frontier.append((link, depth + 1))

    log.info("crawl_done", start=start_url, pages=len(pages))
    return pages[:max_pages]
