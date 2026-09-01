"""Output collector — collect and validate sandbox outputs.

Separated from main.py for testability and reuse.
"""

import os
import logging
import base64
import mimetypes
from typing import Optional

logger = logging.getLogger(__name__)


def collect_and_validate(output_dir: str, expected_types: Optional[list] = None) -> dict:
    """Collect output files and validate against expected types.

    Returns:
        {
            "files": [{"file_name", "mime_type", "data_base64", "file_size"}],
            "validation": {"passed": bool, "errors": [str]},
        }
    """
    files = []
    validation_errors = []

    if not os.path.exists(output_dir):
        return {
            "files": [],
            "validation": {"passed": False, "errors": ["Output directory does not exist"]},
        }

    for root, dirs, filenames in os.walk(output_dir):
        for fname in filenames:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, output_dir)

            with open(fpath, "rb") as f:
                data = f.read()

            mime_type, _ = mimetypes.guess_type(fname)
            if not mime_type:
                mime_type = "application/octet-stream"

            # Validate file is not empty
            if len(data) == 0:
                validation_errors.append(f"Output file '{rel_path}' is empty")
                continue

            files.append({
                "file_name": rel_path,
                "mime_type": mime_type,
                "data_base64": base64.b64encode(data).decode(),
                "file_size": len(data),
            })

    # Check expected types
    if expected_types:
        found_types = {f["mime_type"] for f in files}
        for expected in expected_types:
            if expected not in found_types:
                validation_errors.append(f"Expected output type '{expected}' not found")

    return {
        "files": files,
        "validation": {
            "passed": len(validation_errors) == 0,
            "errors": validation_errors,
        },
    }
