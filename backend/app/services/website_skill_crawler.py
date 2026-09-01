"""Crawl a website and harvest every ``github.com`` link it surfaces.

Added 2026-07-30 so that adding a non-GitHub website URL to the
marketplace (e.g. ``https://awesomeskill.ai/``) actually browses the
site, follows each skill's GitHub link, and feeds the upstream
``skill_source_service._collect_skills_from_github_links`` with real
repo / tree / blob URLs. The previous behavior (``_sync_web_page``
extracted ONE skill via LLM summarization of the page text) becomes
the fallback when the crawl finds no GitHub links at all.

Design
------
* BFS over same-domain pages from the start URL.
* ``agent_browser`` is injected so tests can simulate any page shape
  without touching the real browser binary. The default wrapper binds
  the real tool to a unique ``conversation_id`` so concurrent syncs
  never share a session.
* Per page: try ``eval`` to harvest every ``<a href>`` (works for
  JS-rendered sites like awesomeskill.ai). Fall back to ``extract``
  rendered markdown + regex when ``eval`` returns no hrefs.
* Normalize URLs (strip fragments, ``utm_*`` params, trailing slash,
  ``www.`` prefix) so the BFS doesn't re-visit equivalent pages.
* Skip static asset URLs (``.pdf`` / ``.zip`` / images / fonts …) and
  auth-ish paths (``/login`` / ``/signup`` / ``/auth`` …) when queuing
  the next hop — the GitHub link on the page is still harvested, just
  not navigated to.
* The browser session is closed in a ``finally`` block, even when an
  unexpected exception escapes the BFS.
* Cap at ``max_pages`` (default 100) so a runaway site can't burn the
  background-sync budget.

Module surface
--------------
* :func:`parse_github_skill_link` — pure; turns a GitHub URL into a
  ``GithubLinkInfo`` (owner/repo/branch/subpath/is_file) or ``None``.
* :func:`crawl_site_for_github_links` — async; returns
  ``{page_url: [github_links]}`` for the visited pages.
"""
import logging
import re
import uuid
from typing import Any, Awaitable, Callable, NamedTuple, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)


class GithubLinkInfo(NamedTuple):
    """Result of :func:`parse_github_skill_link`.

    ``subpath`` is the directory path for ``/tree/`` links, the file
    path for ``/blob/`` links, or ``""`` for a bare repo link.
    ``is_file`` distinguishes a single-file link (the caller should
    fetch that exact path) from a directory/bare-repo link (the
    caller should fetch the repo tree and find SKILL.md under the
    subpath).
    """
    owner: str
    repo: str
    branch: str
    subpath: str
    is_file: bool


# Matches the GitHub URL shapes we care about:
#   https://github.com/owner/repo
#   https://github.com/owner/repo.git
#   https://github.com/owner/repo/tree/<branch>[/<subpath>]
#   https://github.com/owner/repo/blob/<branch>/<file>
_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[^/]+)/"
    r"(?P<repo>[^/]+?)(?:\.git)?"
    r"(?:/(?P<kind>tree|blob)/(?P<branch>[^/]+)(?:/(?P<subpath>.+?))?)?"
    r"/?$"
)


def parse_github_skill_link(url: str) -> Optional[GithubLinkInfo]:
    """Parse a GitHub URL into a ``GithubLinkInfo`` or ``None``.

    Non-GitHub URLs and unparseable GitHub URLs return ``None`` so
    callers can filter silently. Bare repo URLs default ``branch`` to
    ``"main"`` and ``subpath`` to ``""`` (meaning the whole repo).
    """
    if not url or not isinstance(url, str):
        return None
    m = _GITHUB_URL_RE.match(url.strip())
    if not m:
        return None
    kind = m.group("kind")
    subpath = m.group("subpath") or ""
    return GithubLinkInfo(
        owner=m.group("owner"),
        repo=m.group("repo"),
        branch=m.group("branch") or "main",
        subpath=subpath,
        is_file=(kind == "blob"),
    )


# ─── URL normalization + classification ────────────────────────────────


_STATIC_EXTENSIONS = frozenset({
    # images
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp",
    # archives
    "pdf", "zip", "gz", "tar", "rar", "7z",
    # web assets
    "css", "js", "map",
    # fonts
    "woff", "woff2", "ttf", "eot", "otf",
    # media
    "mp4", "mp3", "wav", "mov", "avi", "mpg", "mpeg", "webm",
    # office
    "xls", "xlsx", "doc", "docx", "ppt", "pptx",
    # feeds / data
    "xml", "rss", "atom",
})

# Path substrings that smell like auth flows. Matched against the
# lowercased path. We deliberately use a small allowlist of common
# tokens; full route blocking would be a separate concern.
_AUTH_PATH_TOKENS = ("/login", "/signup", "/signin", "/register", "/logout", "/auth", "/sign-up", "/log-in")

_TRACKING_PARAM_PREFIXES = ("utm_",)


def _normalize_url(url: str) -> str:
    """Strip fragment, ``utm_*`` query params, trailing slash, ``www.``.

    Two URLs that differ only by these are equivalent for crawling
    purposes; normalizing them lets the BFS visit each page at most
    once.
    """
    u = urlparse(url)
    if not u.scheme or not u.netloc:
        return url
    scheme = u.scheme.lower()
    netloc = u.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Drop tracking params; keep the rest in stable order.
    q = [
        (k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(q)
    path = u.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", query, ""))


def _is_static_url(url: str) -> bool:
    """Return True for URLs that look like static asset downloads."""
    p = urlparse(url)
    last = p.path.rsplit("/", 1)[-1].lower()
    if "." in last:
        ext = last.rsplit(".", 1)[-1]
        return ext in _STATIC_EXTENSIONS
    return False


def _is_auth_path(url: str) -> bool:
    """Return True if the URL path looks like an auth flow."""
    path = urlparse(url).path.lower()
    return any(tok in path for tok in _AUTH_PATH_TOKENS)


def _is_github_url(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc == "github.com"


def _same_domain(url: str, base_netloc: str) -> bool:
    n = urlparse(url).netloc.lower()
    if n.startswith("www."):
        n = n[4:]
    return n == base_netloc


# ─── link harvest helpers ─────────────────────────────────────────────


_EVAL_HREF_EXPR = (
    "JSON.stringify("
    "Array.from(document.querySelectorAll('a[href]'))"
    ".map(a => a.href)"
    ")"
)

# Matches [text](href) — the standard agent-browser markdown link.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


def _hrefs_from_eval_result(eval_result: Any) -> list[str]:
    """Normalize the result of an ``eval`` call into a list of hrefs.

    The real agent-browser CLI emits JSON for ``eval``; ``_maybe_json``
    inside the tool parses it. When the expression returns a JSON
    array (our default), the result is a list. When it returns a raw
    string (defensive), split on commas.
    """
    if isinstance(eval_result, list):
        return [str(h) for h in eval_result if isinstance(h, str)]
    if isinstance(eval_result, str):
        return [s.strip() for s in eval_result.split(",") if s.strip()]
    return []


def _hrefs_from_markdown(text: str) -> list[str]:
    """Fallback harvester: pull ``(href)`` out of markdown link syntax."""
    return [href for _, href in _MARKDOWN_LINK_RE.findall(text or "")]


# ─── main entry point ─────────────────────────────────────────────────


# Type alias for the injected browser function. The real
# ``_agent_browser`` matches this signature when called with
# ``context={"conversation_id": cid}``; tests pass a simpler callable
# that ignores the conversation_id.
BrowserFn = Callable[[dict], Awaitable[dict]]


async def crawl_site_for_github_links(
    start_url: str,
    *,
    max_pages: int = 100,
    agent_browser: Optional[BrowserFn] = None,
) -> dict[str, list[str]]:
    """BFS same-domain crawl. Returns ``{page_url: [github_links]}``.

    The dictionary records every page the crawl visited (even ones
    that yielded no GitHub links) so the orchestrator can tell apart
    "we visited 3 pages and found nothing" from "we never visited".
    Pages with no links are returned as an empty list under their
    key.

    Stops visiting once ``max_pages`` is reached. The browser session
    is closed in a ``finally`` block even when an unexpected
    exception escapes the BFS.
    """
    if agent_browser is None:
        agent_browser = _default_browser_wrapper()

    start = _normalize_url(start_url)
    base_netloc = urlparse(start).netloc  # already lowercased + www-stripped
    visited: set[str] = set()
    queue: list[str] = [start]
    result: dict[str, list[str]] = {}

    try:
        while queue and len(visited) < max_pages:
            page = queue.pop(0)
            if page in visited:
                continue
            visited.add(page)
            # Don't navigate to assets / auth pages — the links on
            # the *current* page were already harvested, so nothing
            # is lost by skipping these.
            if _is_static_url(page) or _is_auth_path(page):
                continue

            nav = await agent_browser({"action": "navigate", "url": page})
            if not nav.get("success"):
                logger.warning(
                    "Crawl: navigate failed for %s: %s", page, nav.get("error")
                )
                continue

            # Primary: eval to harvest every anchor href. Works for
            # JS-rendered sites where the markdown rendering misses
            # links.
            hrefs: list[str] = []
            ev = await agent_browser({
                "action": "eval",
                "expression": _EVAL_HREF_EXPR,
            })
            if ev.get("success"):
                hrefs = _hrefs_from_eval_result(ev.get("result"))

            # Fallback: rendered markdown + regex. The agent-browser
            # tool returns the page as markdown where links look like
            # ``[text](href)``.
            if not hrefs:
                ex = await agent_browser({"action": "extract", "url": page})
                if ex.get("success"):
                    hrefs = _hrefs_from_markdown(ex.get("text", ""))

            # Normalize + dedupe + classify.
            normalized_seen: set[str] = set()
            normalized: list[str] = []
            for h in hrefs:
                nh = _normalize_url(h)
                if nh in normalized_seen:
                    continue
                normalized_seen.add(nh)
                normalized.append(nh)

            page_github: list[str] = []
            for nh in normalized:
                if _is_github_url(nh):
                    page_github.append(nh)
                elif _same_domain(nh, base_netloc):
                    if _is_static_url(nh) or _is_auth_path(nh):
                        continue
                    if nh not in visited:
                        queue.append(nh)
            result[page] = page_github

        return result
    finally:
        # Close the browser session even when the BFS raises. The
        # real tool's ``close`` action is a no-op when the session
        # was never opened; failures here are best-effort and must
        # not mask the original exception.
        try:
            await agent_browser({"action": "close"})
        except Exception:
            logger.exception("Crawl: failed to close browser for %s", start_url)


def _default_browser_wrapper() -> BrowserFn:
    """Return a browser callable bound to a fresh conversation id.

    The agent-browser tool keys sessions by ``conversation_id`` (see
    ``app.services.tool_handlers.agent_browser_tool``). Binding a
    unique id per crawl means concurrent syncs never share a browser
    session and the per-crawl ``close`` cleans up exactly the session
    that crawl opened.
    """
    from app.services.tool_handlers.agent_browser_tool import _agent_browser

    cid = f"marketplace-crawl-{uuid.uuid4().hex[:8]}"

    async def _default(args: dict) -> dict:
        return await _agent_browser(args, context={"conversation_id": cid})

    return _default
