"""Tool security utilities — shared across all tool handlers.

Provides:
  - is_safe_url():        SSRF protection (blocks private/internal IPs, cloud metadata)
  - validate_path():      Path traversal prevention (confines to workspace)
  - truncate_output():    Output size limits (protects LLM context window)
  - redact_secrets():     Scrub API keys / tokens from tool output
  - scan_memory_content(): Detect prompt-injection patterns in memory writes

Adapted from Hermes url_safety.py and file_safety patterns, reimplemented
for Zhanlu's stack (no hermes_cli / plugins dependencies).
"""

import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSRF Protection
# ---------------------------------------------------------------------------

# Cloud metadata hostnames — always blocked, no toggle
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

# Cloud metadata IPs — always blocked even with allow_private_urls
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS/GCP/Azure metadata
    ipaddress.ip_address("169.254.170.2"),       # AWS ECS task metadata
    ipaddress.ip_address("169.254.169.253"),    # Azure IMDS
    ipaddress.ip_address("fd00:ec2::254"),      # AWS metadata IPv6
    ipaddress.ip_address("100.100.100.200"),    # Alibaba Cloud metadata
    # IPv4-mapped IPv6 variants
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})

_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),          # Entire link-local range
    ipaddress.ip_network("::ffff:169.254.0.0/112"),   # IPv4-mapped link-local
)

# CGNAT range (RFC 6598) — not covered by ipaddress.is_private
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP is private/internal/loopback/metadata."""
    # Handle IPv4-mapped IPv6 addresses
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        embedded = ip.ipv4_mapped
        return (embedded.is_private or embedded.is_loopback or
                embedded.is_link_local or embedded.is_reserved or
                embedded.is_multicast or embedded.is_unspecified or
                embedded in _CGNAT_NETWORK)

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    if ip in _CGNAT_NETWORK:
        return True
    return False


def is_safe_url(url: str) -> bool:
    """Return True if the URL does not target a private/internal address.

    Fails closed: DNS errors and parse errors block the request.
    Cloud metadata endpoints are always blocked regardless of config.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()

        if scheme not in {"http", "https"}:
            logger.warning("Blocked URL — unsupported scheme: %s", scheme or "<empty>")
            return False
        if not hostname:
            return False

        # Always block known metadata hostnames
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("Blocked URL — metadata hostname: %s", hostname)
            return False

        # Try parsing as literal IP first
        try:
            ip = ipaddress.ip_address(hostname)
            if ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS):
                logger.warning("Blocked URL — cloud metadata IP: %s", hostname)
                return False
            if _is_blocked_ip(ip):
                logger.warning("Blocked URL — private/internal IP: %s", hostname)
                return False
            return True
        except ValueError:
            pass  # Not a literal IP, proceed to DNS resolution

        # DNS resolution
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            logger.warning("Blocked URL — DNS resolution failed: %s", hostname)
            return False

        for _family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                resolved = ipaddress.ip_address(ip_str)
            except ValueError:
                continue

            if resolved in _ALWAYS_BLOCKED_IPS or any(resolved in net for net in _ALWAYS_BLOCKED_NETWORKS):
                logger.warning("Blocked URL — %s resolves to metadata IP %s", hostname, ip_str)
                return False
            if _is_blocked_ip(resolved):
                logger.warning("Blocked URL — %s resolves to private IP %s", hostname, ip_str)
                return False

        return True

    except Exception as exc:
        logger.warning("Blocked URL — safety check error for %s: %s", url, exc)
        return False


# ---------------------------------------------------------------------------
# Path Safety
# ---------------------------------------------------------------------------

def validate_path(file_path: str, workspace: str | Path) -> Path:
    """Validate that file_path stays within the workspace directory.

    Returns the resolved absolute Path if safe.
    Raises ValueError if the path escapes the workspace.
    """
    workspace = Path(workspace).resolve()
    target = Path(workspace, file_path).resolve() if not os.path.isabs(file_path) else Path(file_path).resolve()

    # Ensure the resolved target is within the workspace
    try:
        target.relative_to(workspace)
    except ValueError:
        raise ValueError(
            f"Path traversal blocked: '{file_path}' resolves outside the workspace"
        )

    return target


# ---------------------------------------------------------------------------
# Output Size Limits
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = 8000  # Cap tool output to protect LLM context window


def truncate_output(data, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate a string or dict to max_chars, appending a notice if cut.

    If data is a dict, it's JSON-serialized first.
    """
    if isinstance(data, dict):
        import json
        text = json.dumps(data, ensure_ascii=False, default=str)
    elif isinstance(data, str):
        text = data
    else:
        text = str(data)

    if len(text) <= max_chars:
        return text

    return text[:max_chars] + f"\n...[truncated: {len(text) - max_chars} chars omitted]"


# ---------------------------------------------------------------------------
# Secret Redaction
# ---------------------------------------------------------------------------

# Patterns for common API keys, tokens, and credentials
_SECRET_PATTERNS = [
    # OpenAI-style keys: sk-...
    re.compile(r'(sk-[a-zA-Z0-9]{20,})', re.IGNORECASE),
    # Bearer tokens
    re.compile(r'(Bearer\s+[a-zA-Z0-9\-._~+/]+=*)', re.IGNORECASE),
    # Generic API key patterns: key=..., api_key=..., token=...
    re.compile(r'((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*["\']?[a-zA-Z0-9\-_]{16,})', re.IGNORECASE),
    # AWS access keys
    re.compile(r'(AKIA[0-9A-Z]{16})'),
    # JWT tokens (header.payload.signature)
    re.compile(r'(eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)'),
]


def redact_secrets(text: str) -> str:
    """Scrub API keys, tokens, and credentials from a string."""
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text


# ---------------------------------------------------------------------------
# Memory Injection Scanning
# ---------------------------------------------------------------------------

# Patterns that may indicate prompt injection in memory content
_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?previous\s+instructions', re.IGNORECASE),
    re.compile(r'forget\s+(everything|all|previous)', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+(a|an)\s+(different|new)', re.IGNORECASE),
    re.compile(r'system\s*:\s*', re.IGNORECASE),
    re.compile(r'<\|im_start\|>', re.IGNORECASE),
    re.compile(r'<\|im_end\|>', re.IGNORECASE),
    re.compile(r'\[INST\]', re.IGNORECASE),
    re.compile(r'\[/INST\]', re.IGNORECASE),
    re.compile(r'disregard\s+(all\s+)?(prior|previous|above)', re.IGNORECASE),
    re.compile(r'pretend\s+you\s+are', re.IGNORECASE),
    re.compile(r'act\s+as\s+if\s+you\s+(are|were)', re.IGNORECASE),
]


def scan_memory_content(content: str) -> tuple[bool, list[str]]:
    """Scan memory content for prompt injection patterns.

    Returns:
        (is_safe, detected_patterns) — is_safe is False if injection detected.
    """
    if not content:
        return True, []

    detected = []
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(content)
        if match:
            detected.append(match.group())

    if detected:
        logger.warning("Memory injection patterns detected: %s", detected)
        return False, detected

    return True, []
