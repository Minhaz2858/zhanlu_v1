"""Email Notification Gateway package.

Public API for the executor hook points and the API router:

- ``notify_run_finished`` — fire-and-forget run result email.
- ``is_valid_email`` / ``parse_emails`` — email validation + parsing helpers.
- ``generate_download_token`` / ``verify_download_token`` / ``build_download_url``
  — HMAC-signed, time-limited download links.
"""

from .download_link import build_download_url, generate_download_token, verify_download_token
from .gateway import is_valid_email, notify_run_finished, parse_emails

__all__ = [
    "notify_run_finished",
    "is_valid_email",
    "parse_emails",
    "generate_download_token",
    "verify_download_token",
    "build_download_url",
]
