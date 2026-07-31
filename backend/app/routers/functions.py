"""Function router — real document generation and URL skill collection."""

import base64
import json
import os
import re
import uuid
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models.tool import Tool
from app.services.document_service import generate_docx, generate_pptx

router = APIRouter(tags=["functions"])


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _is_github_url(url: str) -> bool:
    """Detect whether a URL points to GitHub."""
    parsed = urlparse(url)
    return parsed.netloc in ("github.com", "www.github.com")


def _is_npm_url(url: str) -> bool:
    """Detect whether a URL points to npm / npmjs.com."""
    parsed = urlparse(url)
    return parsed.netloc in ("www.npmjs.com", "npmjs.com", "registry.npmjs.org")


def _parse_github_url(url: str) -> dict:
    """Parse a GitHub URL into structured parts.

    Handles:
      - https://github.com/owner/repo
      - https://github.com/owner/repo/tree/branch/path
      - https://github.com/owner/repo/blob/branch/path/to/file.md
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return {"error": f"Invalid GitHub URL: {url}"}

    result = {"owner": parts[0], "repo": parts[1].removesuffix(".git"), "branch": "main", "subpath": ""}

    if len(parts) > 2 and parts[2] in ("tree", "blob"):
        if len(parts) >= 4:
            result["branch"] = parts[3]
        if len(parts) > 4:
            result["subpath"] = "/".join(parts[4:])

    return result


def _url_to_name(url: str) -> str:
    """Derive a human-readable name from a URL."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path:
        parts = path.split("/")
        name = parts[-1]
        # Clean up common extensions
        for ext in (".html", ".md", ".htm"):
            if name.endswith(ext):
                name = name[:-len(ext)]
        # Replace hyphens/underscores with spaces, title-case
        name = re.sub(r"[-_]", " ", name).strip()
        if name:
            return " ".join(w.capitalize() for w in name.split())
    return parsed.netloc.replace("www.", "")


# ---------------------------------------------------------------------------
# HTML / content extraction helpers
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Lightweight HTML-to-text extractor — strips tags, keeps readable text."""

    def __init__(self):
        super().__init__()
        self._text: list[str] = []
        self._skip_tags = {"script", "style", "noscript", "nav", "footer", "header"}

    def handle_starttag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self._text.append("\n")

    def handle_data(self, data):
        stripped = data.strip()
        if stripped:
            self._text.append(stripped)
            self._text.append(" ")

    def get_text(self) -> str:
        raw = "".join(self._text)
        # Collapse whitespace
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r" {2,}", " ", raw)
        return raw.strip()


def _extract_text_from_html(html: str) -> str:
    """Extract readable text from HTML."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.get_text()[:50000]  # Cap at 50k chars


def _extract_title_from_html(html: str, fallback_url: str = "") -> str:
    """Extract the <title> from HTML."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title:
            return title[:200]
    if fallback_url:
        return _url_to_name(fallback_url)
    return "Untitled Skill"


def _extract_meta_description(html: str) -> str:
    """Extract meta description from HTML."""
    m = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()[:500]
    return ""


def _parse_markdown_skills(content: str) -> list[dict]:
    """Try to split a markdown page into individual skill blocks.

    Heuristic: look for headings that look like skill names, then grab
    the content until the next heading of the same or higher level.
    """
    skills = []
    # Find all H1/H2 headings
    heading_pattern = re.compile(
        r"(?:^|\n)(#{1,3})\s+(.+?)(?:\n|$)", re.MULTILINE
    )
    headings = list(heading_pattern.finditer(content))

    if len(headings) <= 1:
        return []  # Not enough structure to split

    for i, m in enumerate(headings):
        name = m.group(2).strip()
        # Skip obvious non-skill headings
        if re.match(
            r"(?i)^(overview|introduction|getting.?started|installation|"
            r"api.?reference|contributing|license|changelog|table.?of.?contents|"
            r"index|home|documentation|resources|acknowledgements|faq|toc)$",
            name,
        ):
            continue

        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(content)
        body = content[start:end].strip()

        # Only include if body has some substance
        if len(body) > 50:
            # Derive description from first paragraph
            desc_match = re.search(r"^(.+?)(?:\n\n|\n(?=#)|$)", body, re.DOTALL)
            description = desc_match.group(1).strip()[:300] if desc_match else ""
            trigger = re.sub(r"[^\w-]", "", name.lower().replace(" ", "-"))[:50]

            skills.append({
                "name": name[:200],
                "description": description,
                "trigger": trigger,
                "category": "imported",
                "content": f"# {name}\n\n{body}",
            })

    return skills


# ---------------------------------------------------------------------------
# GitHub skill collection
# ---------------------------------------------------------------------------

def _make_github_headers() -> dict:
    """Build GitHub API headers, optionally with token auth."""
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "Zhanlu-SkillAgent/1.0"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


SKILL_MD_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^SKILL\.md$",                                    # exact SKILL.md
        r"^skill\.md$",                                     # lowercase
        r"^skills?\.md$",                                   # skills.md / skill.md
        r"^[a-zA-Z0-9_-]+skill\.md$",                       # *-skill.md, *_skill.md
        r"^skill[_-][a-zA-Z0-9_-]+\.md$",                   # skill-*.md
    ]
]


def _is_skill_file(name: str) -> bool:
    """Check if a filename looks like a skill definition file."""
    return any(p.match(name) for p in SKILL_MD_PATTERNS)


async def _github_find_skill_files(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    depth: int = 0,
) -> list[dict]:
    """Recursively find SKILL.md files in a GitHub directory."""
    if depth > 5:
        return []
    skill_files = []
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        items = resp.json()
        if not isinstance(items, list):
            return []
        for item in items:
            item_type = item.get("type")
            name = item.get("name", "")
            if item_type == "file" and _is_skill_file(name):
                skill_files.append(item)
            elif item_type == "dir" and not name.startswith(".") and name not in (
                "node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build",
            ):
                sub_files = await _github_find_skill_files(
                    client, item.get("url", ""), headers, depth + 1,
                )
                skill_files.extend(sub_files)
    except Exception:
        pass
    return skill_files


async def _github_fetch_content(
    client: httpx.AsyncClient, item: dict, headers: dict,
) -> str | None:
    """Fetch and decode the content of a GitHub file item."""
    try:
        # Prefer download_url when available
        download_url = item.get("download_url")
        if download_url:
            resp = await client.get(download_url, headers=headers)
            if resp.status_code == 200:
                return resp.text
        # Fall back to contents API (base64)
        resp = await client.get(item.get("url", ""), headers=headers)
        data = resp.json()
        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def _parse_skill_md(content: str) -> dict:
    """Parse a SKILL.md file into name, description, trigger, and body."""
    name = "Untitled Skill"
    description = ""
    trigger = ""

    # Try to extract H1 as name
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if m:
        name = m.group(1).strip()[:200]
        trigger = re.sub(r"[^\w-]", "", name.lower().replace(" ", "-"))[:50]

    # Try YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).split("\n"):
            line = line.strip()
            if ":" in line:
                key, val = line.split(":", 1)
                key, val = key.strip().lower(), val.strip().strip("\"'")
                if key == "name" and val:
                    name = val[:200]
                    trigger = re.sub(r"[^\w-]", "", name.lower().replace(" ", "-"))[:50]
                elif key == "description" and val:
                    description = val[:500]
                elif key == "trigger" and val:
                    trigger = val[:100]

    # Fallback: first non-empty paragraph as description
    if not description:
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip() and not p.strip().startswith("#")]
        if paragraphs:
            description = paragraphs[0][:500]

    return {"name": name, "description": description, "trigger": trigger}


async def _collect_from_github(url: str, kind: str, db: Session) -> dict:
    """Collect skills from a GitHub repository and auto-save to database."""
    parsed = _parse_github_url(url)
    if "error" in parsed:
        return {"success": False, "collected": 0, "error": parsed["error"]}

    owner = parsed["owner"]
    repo = parsed["repo"]
    branch = parsed.get("branch", "main")
    subpath = parsed.get("subpath", "")
    headers = _make_github_headers()

    has_token = bool(os.environ.get("GITHUB_TOKEN", ""))

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Build the starts-with URL for directory listing
        if subpath:
            api_url = (
                f"https://api.github.com/repos/{owner}/{repo}/contents/{subpath}"
                f"?ref={branch}"
            )
        else:
            api_url = (
                f"https://api.github.com/repos/{owner}/{repo}/contents"
                f"?ref={branch}"
            )

        # Verify repo is accessible
        try:
            check = await client.get(api_url, headers=headers)
        except httpx.RequestError as e:
            return {
                "success": False, "collected": 0,
                "error": f"GitHub API request failed: {e}",
            }

        if check.status_code == 404:
            return {
                "success": False, "collected": 0,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
            }
        if check.status_code == 403 and not has_token:
            return {
                "success": False, "collected": 0,
                "error": (
                    "GitHub API rate limit exceeded (60 requests/hour without a token). "
                    "Set GITHUB_TOKEN environment variable for higher limits."
                ),
            }
        if check.status_code >= 400:
            return {
                "success": False, "collected": 0,
                "error": f"GitHub API returned status {check.status_code}",
            }

        # Find SKILL.md files recursively
        skill_files = await _github_find_skill_files(client, api_url, headers)

        if not skill_files:
            return {
                "success": True, "collected": 0, "skills": [], "errors": [],
                "info": "No SKILL.md files found in this repository.",
            }

        collected_skills: list[dict] = []
        errors: list[str] = []

        for item in skill_files:
            try:
                content = await _github_fetch_content(client, item, headers)
                if not content:
                    errors.append(f"Could not fetch content for {item.get('name')}")
                    continue

                parsed_skill = _parse_skill_md(content)

                tool = Tool(
                    name=parsed_skill["name"],
                    description=parsed_skill["description"][:500],
                    trigger=parsed_skill["trigger"],
                    skill_md=content,
                    kind=kind,
                    source="github",
                    category="imported",
                    publisher=owner,
                    github_url=item.get("html_url", url),
                    status="active",
                    enabled=True,
                    app_id="default-app",
                )
                db.add(tool)
                db.flush()  # Get the id without committing yet

                collected_skills.append({
                    "id": tool.id,
                    "name": tool.name,
                    "description": (tool.description or "")[:100],
                })

            except Exception as e:
                errors.append(f"{item.get('name', 'unknown')}: {str(e)}")

        if collected_skills:
            db.commit()

        return {
            "success": True,
            "collected": len(collected_skills),
            "skills": collected_skills,
            "errors": errors,
        }


# ---------------------------------------------------------------------------
# General website skill collection
# ---------------------------------------------------------------------------

async def _collect_from_website(url: str, kind: str, db: Session) -> dict:
    """Collect skills from a general website by extracting content."""
    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True,
        ) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; Zhanlu-SkillAgent/1.0; "
                        "+https://zhanlu.dev)"
                    ),
                },
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()

            if "application/json" in content_type:
                # API endpoint — try to parse as JSON
                try:
                    data = resp.json()
                    text = json.dumps(data, indent=2, ensure_ascii=False)
                except Exception:
                    text = resp.text
            elif "text/markdown" in content_type or url.endswith((".md", ".markdown")):
                text = resp.text
            else:
                text = _extract_text_from_html(resp.text)

        if not text or len(text.strip()) < 20:
            return {
                "success": False, "collected": 0,
                "error": "Could not extract meaningful content from the page.",
            }

        # Try to parse multiple skills from the content
        skills = _parse_markdown_skills(text)
        collected_skills: list[dict] = []
        errors: list[str] = []

        if skills and len(skills) > 1:
            # Multiple skills found
            for skill in skills:
                try:
                    tool = Tool(
                        name=skill["name"],
                        description=skill["description"][:500],
                        trigger=skill["trigger"],
                        skill_md=skill["content"],
                        kind=kind,
                        source="web",
                        category=skill.get("category", "imported"),
                        publisher="web",
                        github_url=url,
                        status="active",
                        enabled=True,
                        app_id="default-app",
                    )
                    db.add(tool)
                    db.flush()
                    collected_skills.append({
                        "id": tool.id,
                        "name": skill["name"],
                        "description": skill["description"][:100],
                    })
                except Exception as e:
                    errors.append(f"{skill['name']}: {str(e)}")

            if collected_skills:
                db.commit()
        else:
            # Single skill from the whole page
            name = _extract_title_from_html(
                resp.text if "text/html" in content_type else "", url,
            )
            description = _extract_meta_description(
                resp.text if "text/html" in content_type else "",
            ) or text[:500].replace("\n", " ").strip()

            trigger = re.sub(r"[^\w-]", "", name.lower().replace(" ", "-"))[:50]

            try:
                tool = Tool(
                    name=name,
                    description=description[:500],
                    trigger=trigger,
                    skill_md=text,
                    kind=kind,
                    source="web",
                    category="imported",
                    publisher="web",
                    github_url=url,
                    status="active",
                    enabled=True,
                    app_id="default-app",
                )
                db.add(tool)
                db.commit()
                db.refresh(tool)
                collected_skills.append({
                    "id": tool.id,
                    "name": name,
                    "description": description[:100],
                })
            except Exception as e:
                errors.append(str(e))

        return {
            "success": True,
            "collected": len(collected_skills),
            "skills": collected_skills,
            "errors": errors,
            "source": "web",
        }

    except httpx.HTTPStatusError as e:
        return {
            "success": False, "collected": 0,
            "error": f"HTTP {e.response.status_code}: Could not fetch the page.",
        }
    except httpx.RequestError as e:
        return {
            "success": False, "collected": 0,
            "error": f"Request failed: {str(e)}",
        }
    except Exception as e:
        return {
            "success": False, "collected": 0,
            "error": f"Collection failed: {str(e)}",
        }


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@router.post("/apps/{app_id}/functions/collectSkills")
async def collect_skills_from_url(
    app_id: str,
    body: dict = None,
    db: Session = Depends(get_db),
):
    """Collect skills from any URL — auto-detects source type and saves to database.

    Accepts:
      - GitHub URLs (repos, directories, file trees)
      - General website URLs (web pages, markdown files, API endpoints)

    Auto-detects the URL type and uses the appropriate strategy:
      - GitHub: Recursively searches for SKILL.md files and imports them
      - Website: Extracts page content and creates skill records

    All discovered skills are saved to the Tool table immediately.
    """
    body = body or {}
    url = (body.get("url") or body.get("repo_url") or "").strip()
    kind = body.get("kind", "system_skill")

    if not url:
        return {"success": False, "collected": 0, "error": "url is required"}

    # Step 1: Detect URL type
    if _is_github_url(url):
        return await _collect_from_github(url, kind, db)
    else:
        return await _collect_from_website(url, kind, db)


# Keep backward-compatible endpoint
@router.post("/apps/{app_id}/functions/collectGithubSkills")
async def collect_github_skills(
    app_id: str,
    body: dict = None,
    db: Session = Depends(get_db),
):
    """Legacy endpoint — delegates to collectSkills with the same logic."""
    return await collect_skills_from_url(app_id, body, db)


@router.post("/apps/{app_id}/functions/generateReportDocx")
async def generate_report_docx(app_id: str, body: dict = None):
    """Generate a real .docx report file from markdown content.

    Saves the file to the uploads directory and returns the file URL.
    """
    body = body or {}
    title = body.get("title", "Untitled Report")
    markdown = body.get("markdown", "")

    try:
        file_url = generate_docx(title, markdown)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DOCX generation failed: {str(e)}")

    return {
        "file_url": file_url,
        "status": "ready",
        "title": title,
    }


@router.post("/apps/{app_id}/functions/generatePptx")
async def generate_pptx_endpoint(app_id: str, body: dict = None):
    """Generate a real .pptx presentation file from slide data.

    Saves the file to the uploads directory and returns the file URL.
    """
    body = body or {}
    title = body.get("title", "Untitled Presentation")
    slides = body.get("slides", [])

    if not isinstance(slides, list):
        raise HTTPException(status_code=400, detail="slides must be a list of {title, bullets} objects")

    try:
        file_url = generate_pptx(title, slides)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PPTX generation failed: {str(e)}")

    return {
        "file_url": file_url,
        "status": "ready",
        "title": title,
    }
