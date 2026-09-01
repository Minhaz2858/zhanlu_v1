#!/usr/bin/env python3
"""
Zhanlu Database Management Tool
================================
Manage PostgreSQL, Redis, and MinIO for the Zhanlu system.

Usage:
  python scripts/db_manage.py check      Test connectivity to all services
  python scripts/db_manage.py clear      Wipe all data (PG tables, Redis keys, MinIO objects)
  python scripts/db_manage.py setup      Create extensions, run migrations, seed data, create bucket
  python scripts/db_manage.py reset      clear + setup (full database reset)

Environment:
  Reads DATABASE_URL, REDIS_URL, MINIO_* from .env or environment.
"""

import os
import sys
import argparse
import time
from urllib.parse import urlparse, unquote

# Ensure backend is on path so we can import app modules
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND_DIR)


def load_dotenv():
    """Load .env file from project root."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = value


load_dotenv()

# ── Configuration from .env ──────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", os.environ.get("MINIO_ROOT_USER", "minioadmin"))
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"))
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "zhanlu-artifacts")

BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def ok(msg):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg):
    print(f"  {RED}✗{RESET} {msg}")


def info(msg):
    print(f"  {CYAN}→{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠{RESET} {msg}")


# ═══════════════════════════════════════════════════════════════════════════
# CHECK
# ═══════════════════════════════════════════════════════════════════════════

def check_postgresql() -> bool:
    """Test PostgreSQL connectivity."""
    print(f"{BOLD}PostgreSQL{RESET}")
    url = DATABASE_URL
    if not url:
        fail("DATABASE_URL is not set")
        return False
    try:
        import psycopg2
        parsed = urlparse(url)
        dbname = parsed.path.lstrip("/")
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432

        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password, connect_timeout=10
        )
        cur = conn.cursor()
        cur.execute("SELECT version();")
        ver = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM information_schema.tables
                       WHERE table_schema='public';""")
        table_count = cur.fetchone()[0]
        cur.close()
        conn.close()
        ok(f"Connected — {ver[:50]}...  [{table_count} table(s)]")
        return True
    except Exception as e:
        fail(str(e))
        return False


def check_redis() -> bool:
    """Test Redis connectivity."""
    print(f"{BOLD}Redis{RESET}")
    url = REDIS_URL
    if not url:
        fail("REDIS_URL is not set")
        return False
    try:
        import redis
        r = redis.from_url(url, socket_connect_timeout=10, decode_responses=True)
        r.ping()
        info_srv = r.info("server")
        dbsize = r.dbsize()
        r.close()
        ok(f"Connected — Redis {info_srv.get('redis_version','?')}, keys in db0: {dbsize}")
        return True
    except Exception as e:
        fail(str(e))
        return False


def check_minio() -> bool:
    """Test MinIO connectivity."""
    print(f"{BOLD}MinIO{RESET}")
    if not MINIO_ENDPOINT:
        fail("MINIO_ENDPOINT is not set")
        return False
    try:
        from minio import Minio
        client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=False,
        )
        buckets = client.list_buckets()
        names = [b.name for b in buckets]
        ok(f"Connected — buckets: {names}")
        return True
    except Exception as e:
        fail(str(e))
        return False


# ═══════════════════════════════════════════════════════════════════════════
# CLEAR
# ═══════════════════════════════════════════════════════════════════════════

def clear_postgresql() -> bool:
    """Drop all tables from the public schema."""
    print(f"{BOLD}Clearing PostgreSQL...{RESET}")
    try:
        from app.database import engine, Base

        info("Dropping all tables in public schema...")
        Base.metadata.drop_all(bind=engine)
        ok("All tables dropped")
        return True
    except Exception as e:
        fail(f"Failed to drop tables: {e}")
        return False


def clear_redis() -> bool:
    """Flush all keys from Redis."""
    print(f"{BOLD}Clearing Redis...{RESET}")
    try:
        import redis
        r = redis.from_url(REDIS_URL, socket_connect_timeout=10, decode_responses=True)
        r.flushall()
        r.close()
        ok("FLUSHALL complete")
        return True
    except Exception as e:
        fail(f"Failed: {e}")
        return False


def clear_minio() -> bool:
    """Remove all objects from the zhanlu bucket."""
    print(f"{BOLD}Clearing MinIO...{RESET}")
    try:
        from minio import Minio
        client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=False,
        )

        # Check bucket exists
        if not client.bucket_exists(MINIO_BUCKET):
            warn(f"Bucket '{MINIO_BUCKET}' does not exist — nothing to clear")
            return True

        # List and delete all objects
        objects = client.list_objects(MINIO_BUCKET, recursive=True)
        removed = 0
        errors = []
        for obj in objects:
            try:
                client.remove_object(MINIO_BUCKET, obj.object_name)
                removed += 1
            except Exception as e:
                errors.append(f"  {obj.object_name}: {e}")

        if errors:
            for err in errors[:5]:
                warn(err)
            if len(errors) > 5:
                warn(f"  ... and {len(errors) - 5} more errors")

        ok(f"Removed {removed} object(s) from '{MINIO_BUCKET}'")
        return True
    except Exception as e:
        fail(f"Failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# SETUP
# ═══════════════════════════════════════════════════════════════════════════

def setup_postgresql() -> bool:
    """Create extensions, run alembic migrations, seed data."""
    print(f"{BOLD}Setting up PostgreSQL...{RESET}")
    try:
        import psycopg2
        parsed = urlparse(DATABASE_URL)
        dbname = parsed.path.lstrip("/")
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432

        # Step 1: Create extensions
        info("Creating extensions (uuid-ossp, pg_trgm)...")
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";")
        cur.execute("CREATE EXTENSION IF NOT EXISTS \"pg_trgm\";")
        cur.close()
        conn.close()
        ok("Extensions created")

        # Step 2: Run Alembic migrations
        info("Running alembic upgrade head...")
        os.chdir(BACKEND_DIR)
        from alembic.config import Config
        from alembic import command

        alembic_cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
        command.upgrade(alembic_cfg, "head")
        ok("Alembic migrations complete")

        # Step 3: Seed data
        info("Seeding database...")
        import importlib
        seed_module = importlib.import_module("seed")
        seed_module.seed()
        ok("Seed complete")

        return True
    except Exception as e:
        fail(str(e))
        return False


def setup_redis() -> bool:
    """Redis needs no schema setup — just verify connectivity."""
    print(f"{BOLD}Setting up Redis...{RESET}")
    return check_redis()


def setup_minio() -> bool:
    """Create the zhanlu-artifacts bucket if it doesn't exist."""
    print(f"{BOLD}Setting up MinIO...{RESET}")
    try:
        from minio import Minio
        client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=False,
        )

        if client.bucket_exists(MINIO_BUCKET):
            ok(f"Bucket '{MINIO_BUCKET}' already exists")
        else:
            info(f"Creating bucket '{MINIO_BUCKET}'...")
            client.make_bucket(MINIO_BUCKET)
            ok(f"Bucket '{MINIO_BUCKET}' created")

        # Set anonymous read policy
        try:
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{MINIO_BUCKET}/*"],
                    }
                ],
            }
            import json
            client.set_bucket_policy(MINIO_BUCKET, json.dumps(policy))
            info("Public read policy set")
        except Exception as e:
            warn(f"Could not set bucket policy: {e}")

        return True
    except Exception as e:
        fail(str(e))
        return False


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Zhanlu Database Management")
    parser.add_argument(
        "command",
        choices=["check", "clear", "setup", "reset"],
        help="check=test connectivity | clear=wipe all data | setup=init schema+seed | reset=clear+setup",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  Zhanlu DB Manager — {args.command.upper()}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")

    results = {}

    if args.command == "check":
        results["PostgreSQL"] = check_postgresql()
        results["Redis"] = check_redis()
        results["MinIO"] = check_minio()

    elif args.command == "clear":
        results["PostgreSQL"] = clear_postgresql()
        results["Redis"] = clear_redis()
        results["MinIO"] = clear_minio()

    elif args.command == "setup":
        results["PostgreSQL"] = setup_postgresql()
        results["Redis"] = setup_redis()
        results["MinIO"] = setup_minio()

    elif args.command == "reset":
        # Clear first
        results["PG Clear"] = clear_postgresql()
        results["Redis Clear"] = clear_redis()
        results["MinIO Clear"] = clear_minio()
        print()
        # Then setup
        results["PG Setup"] = setup_postgresql()
        results["Redis Setup"] = setup_redis()
        results["MinIO Setup"] = setup_minio()

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  Summary{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    all_ok = True
    for name, success in results.items():
        status = f"{GREEN}OK{RESET}" if success else f"{RED}FAIL{RESET}"
        print(f"  {status}  {name}")
        if not success:
            all_ok = False

    print()
    if all_ok:
        print(f"  {GREEN}{BOLD}All operations completed successfully.{RESET}")
    else:
        print(f"  {RED}{BOLD}Some operations failed. Check the errors above.{RESET}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
