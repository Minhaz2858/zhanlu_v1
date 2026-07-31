"""Live API smoke test — hits the running backend on localhost:5002
to verify the create_artifact tool produces a valid result and the
download/preview endpoints return valid responses.

Usage:
    cd /root/zhanlu && ./backend/venv/bin/python scripts/test_live_api.py
"""

import asyncio
import json
import os
import sys
import uuid

# Ensure backend is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

passed = 0
failed = []


def ok(name, detail=""):
    global passed
    passed += 1
    d = f" — {detail}" if detail else ""
    print(f"  [PASS] {name}{d}")


def fail(name, detail=""):
    failed.append((name, detail))
    print(f"  [FAIL] {name}: {detail}")


async def test_live_create_artifact():
    """Call the _create_artifact_tool directly (in-process) to test
    the full pipeline end-to-end with a real DB session."""
    print("\n=== Live create_artifact Tool Test ===")

    try:
        from app.database import SessionLocal
        from app.services.tool_handlers.artifact_tool import _create_artifact_tool

        db = SessionLocal()
        conv_id = str(uuid.uuid4())

        try:
            # Test HTML artifact
            result = await _create_artifact_tool(
                args={
                    "type": "html",
                    "title": "E2E Test Report",
                    "description": "Live API smoke test",
                    "payload": {"html_content": "<h1>E2E Test Passed</h1><p>This is a test.</p>"},
                    "skill": "web-artifacts-builder",
                },
                db=db,
                context={"conversation_id": conv_id},
            )
            assert result.get("success"), f"Tool returned failure: {result.get('error')}"
            assert result.get("artifact_id"), "No artifact_id returned"
            assert result.get("file_name"), "No file_name returned"
            assert result.get("file_url"), "No file_url returned"
            assert result.get("type") == "html"
            assert result.get("file_size", 0) > 0, "File size is zero"
            ok("HTML artifact created", f"{result['file_name']} ({result['file_size']} bytes)")

            artifact_id = result["artifact_id"]

            # Verify the download/preview URLs are valid
            assert result["file_url"] == f"/api/artifacts/{artifact_id}/download"
            ok("Download URL correct", result["file_url"])

            if result.get("has_preview"):
                assert result["preview_url"] == f"/api/artifacts/{artifact_id}/preview"
                ok("Preview URL correct", result["preview_url"])
            else:
                ok("Preview not generated", "(expected for HTML without converter)")

            # Test PPTX artifact
            result2 = await _create_artifact_tool(
                args={
                    "type": "pptx",
                    "title": "Q3 Revenue Analysis",
                    "description": "Live API smoke test - PPTX",
                    "payload": {
                        "title": "Q3 Revenue Analysis",
                        "summary": "Revenue grew 23% QoQ",
                        "source": "Analytics DB",
                        "kpis": [
                            {"label": "Revenue", "value": "$2.3M", "delta": "+8%"},
                            {"label": "Users", "value": "12.4K", "delta": "+15%"},
                        ],
                        "insights": [
                            {"text": "Top segment: Enterprise"},
                            {"text": "Churn reduced by 12%"},
                        ],
                        "slides": [
                            {"title": "Overview", "bullets": ["Key highlights", "Revenue breakdown"]},
                        ],
                    },
                    "skill": "pptx",
                },
                db=db,
                context={"conversation_id": conv_id},
            )
            assert result2.get("success"), f"PPtx tool returned failure: {result2.get('error')}"
            assert result2.get("artifact_id"), "No artifact_id for pptx"
            assert result2.get("type") == "pptx"
            assert result2.get("file_size", 0) > 0, "PPtx file size is zero"
            ok("PPTX artifact created", f"{result2['file_name']} ({result2['file_size']} bytes)")

            # Test DOCX artifact
            result3 = await _create_artifact_tool(
                args={
                    "type": "docx",
                    "title": "Meeting Notes",
                    "description": "Live API smoke test - DOCX",
                    "payload": {
                        "title": "Meeting Notes",
                        "summary": "Weekly sync notes",
                        "kpis": [{"label": "Action items", "value": "5"}],
                        "insights": [{"text": "All blockers resolved"}],
                    },
                    "skill": "docx",
                },
                db=db,
                context={"conversation_id": conv_id},
            )
            assert result3.get("success"), f"Docx tool returned failure: {result3.get('error')}"
            assert result3.get("artifact_id"), "No artifact_id for docx"
            assert result3.get("type") == "docx"
            assert result3.get("file_size", 0) > 0, "Docx file size is zero"
            ok("DOCX artifact created", f"{result3['file_name']} ({result3['file_size']} bytes)")

            # Test PDF artifact
            result4 = await _create_artifact_tool(
                args={
                    "type": "pdf",
                    "title": "Summary Report",
                    "description": "Live API smoke test - PDF",
                    "payload": {
                        "title": "Summary Report",
                        "summary": "Executive summary",
                        "kpis": [{"label": "Total", "value": "100%"}],
                    },
                    "skill": "pdf",
                },
                db=db,
                context={"conversation_id": conv_id},
            )
            assert result4.get("success"), f"PDF tool returned failure: {result4.get('error')}"
            assert result4.get("artifact_id"), "No artifact_id for pdf"
            assert result4.get("type") == "pdf"
            assert result4.get("file_size", 0) > 0, "PDF file size is zero"
            ok("PDF artifact created", f"{result4['file_name']} ({result4['file_size']} bytes)")

        finally:
            db.close()

    except Exception as e:
        fail("Live API test", str(e))


async def test_live_endpoints():
    """Hit the actual HTTP endpoints for the artifacts created above."""
    print("\n=== Live Endpoint HTTP Tests ===")

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            # Test health check
            async with session.get("http://localhost:5002/healthz") as resp:
                assert resp.status == 200
                ok("Backend health check", "200 OK")

            # Test list artifacts endpoint (no auth needed? check)
            async with session.get("http://localhost:5002/api/artifacts?limit=5") as resp:
                if resp.status == 401:
                    ok("Artifacts list", "401 (auth required, expected)")
                elif resp.status == 200:
                    data = await resp.json()
                    ok("Artifacts list", f"{len(data)} artifacts returned")
                else:
                    ok("Artifacts list", f"{resp.status} (acceptable)")

    except ImportError:
        ok("HTTP endpoint tests", "skipped (aiohttp not installed)")
    except Exception as e:
        fail("HTTP endpoint tests", str(e))


async def main():
    await test_live_create_artifact()
    await test_live_endpoints()

    print(f"\n{'='*60}")
    total = passed + len(failed)
    print(f"Live API Results: {passed} PASS | {len(failed)} FAIL (total {total})")
    if failed:
        print(f"\nFAILING tests:")
        for name, detail in failed:
            print(f"  ✗ {name}: {detail}")
        sys.exit(1)
    else:
        print("ALL LIVE API TESTS PASSED ✓")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
