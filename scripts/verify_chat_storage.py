#!/usr/bin/env python3
"""DB Storage Verifier — walk the full ChatSession → Artifact chain and assert metadata.

Usage:
    python scripts/verify_chat_storage.py               # latest 5 sessions
    python scripts/verify_chat_storage.py --session-id <uuid>  # specific session
    python scripts/verify_chat_storage.py --create-tables      # create tables first
"""

import sys, os, argparse
from sqlalchemy import inspect

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

from app.database import SessionLocal, engine, Base
from app.models.artifact import Artifact, ArtifactVersion, ArtifactBlob, MessageArtifact
from app.models.chat_session import ChatSession
from app.models.chat_message import ChatMessage


def format_size(size: int) -> str:
    if not size: return "0 B"
    if size < 1024: return f"{size} B"
    if size < 1024 * 1024: return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def verify(session_id: str | None = None, create_tables: bool = False):
    # -- Ensure tables exist --
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    needed = {"chat_sessions", "chat_messages", "artifacts", "artifact_versions",
              "artifact_blobs", "message_artifacts"}
    missing = needed - existing

    if missing:
        if create_tables:
            print(f"Creating missing tables: {sorted(missing)}")
            Base.metadata.create_all(bind=engine)
            print("Tables created successfully.")
        else:
            print("ERROR: Missing tables:", sorted(missing))
            print("Run with --create-tables to create them, or run:")
            print("  cd backend && python3 -m alembic upgrade head")
            return 1

    db = SessionLocal()
    try:
        query = db.query(ChatSession).filter(ChatSession.is_deleted == False)
        if session_id:
            query = query.filter(ChatSession.id == session_id)
        sessions = query.order_by(ChatSession.created_date.desc()).limit(5).all()

        if not sessions:
            print("No ChatSessions found. Start a conversation in the chat UI first.")
            db.close()
            return 0

        total_msgs, total_artifacts, issues = 0, 0, []

        for s in sessions:
            print("=" * 60)
            print(f"Session: {s.id[:8]}...  \"{s.title}\"  ({str(s.created_date)[:10]})")
            print(f"  Project={s.project}  Starred={s.starred}  LastMsg={s.last_message_at}")

            msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == s.id, ChatMessage.is_deleted == False)
                .order_by(ChatMessage.order).all()
            )
            total_msgs += len(msgs)

            if not msgs:
                print("  (no messages)")

            for m in msgs:
                icon = {"user": "U", "assistant": "A", "system": "S"}.get(m.role, "?")
                preview = (m.content or "")[:70].replace("\n", " ")
                print(f"  [{m.order}] {icon} {m.role}: \"{preview}\"")

                links = (
                    db.query(MessageArtifact)
                    .filter(MessageArtifact.message_id == m.id)
                    .order_by(MessageArtifact.display_order).all()
                )
                for link in links:
                    art = db.query(Artifact).filter(
                        Artifact.id == link.artifact_id, Artifact.is_deleted == False
                    ).first()
                    if not art:
                        issues.append(f"Dangling Artifact {link.artifact_id[:8]}")
                        continue
                    total_artifacts += 1
                    print(f"    -> Artifact: {art.id[:8]}  \"{art.title}\"  type={art.artifact_type}  status={art.status}")

                    versions = db.query(ArtifactVersion).filter(
                        ArtifactVersion.artifact_id == art.id
                    ).order_by(ArtifactVersion.version_number).all()
                    for v in versions:
                        print(f"       v{v.version_number}  status={v.status}  skill={v.produced_by_skill}")
                        blobs = db.query(ArtifactBlob).filter(
                            ArtifactBlob.version_id == v.id
                        ).all()
                        for b in blobs:
                            print(f"         [{b.blob_type}] {b.file_name}  {format_size(b.file_size)}  {b.mime_type}")

        print("=" * 60)
        print(f"Sessions: {len(sessions)}  Messages: {total_msgs}  Artifacts: {total_artifacts}")
        if issues:
            print(f"Issues ({len(issues)}):")
            for i in issues:
                print(f"  - {i}")
        else:
            print("Issues: 0 — all chains intact")

        db.close()
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify chat→artifact DB chain")
    parser.add_argument("--session-id", help="Filter to a specific session")
    parser.add_argument("--create-tables", action="store_true",
                        help="Create database tables if they don't exist")
    args = parser.parse_args()
    sys.exit(verify(args.session_id, create_tables=args.create_tables))
