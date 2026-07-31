# ── Zhanlu Makefile ────────────────────────────────────────────────────
# Single-command developer experience.
#
#   make up            Build + start full stack
#   make down          Stop and remove containers
#   make down-v        Stop + remove containers + delete volumes
#   make logs          Tail all service logs
#   make smoke         Run the API E2E smoke test suite
#   make build         Build all images (no start)
#   make reset         Full teardown + rebuild from scratch
#
#   make db-check      Test connectivity to PG/Redis/MinIO
#   make db-clear      Wipe all data (tables, keys, bucket objects)
#   make db-setup      Init schema, run migrations, seed data, create bucket
#   make db-reset      db-clear + db-setup (full database reset)
#   make db-info       Print which database the backend is actually connected to
#                      (engine, host, schema version, row counts per table).
#                      Use this whenever you suspect "the wrong DB" or empty data.

.PHONY: up down down-v logs smoke build reset \
        db-check db-clear db-setup db-reset db-info

# ── Core commands ─────────────────────────────────────────────────────

up:
	@echo "Building sandbox images first..."
	docker compose build sandbox-python sandbox-office sandbox-pptx sandbox-webapp
	@echo "Building + starting full stack..."
	docker compose up -d --build --remove-orphans
	@echo ""
	@echo "Stack starting. Check status:"
	@echo "  make logs          # Tail all logs"
	@echo "  make smoke         # Run E2E smoke test"
	@echo "  http://localhost:5002/healthz"

down:
	docker compose down

down-v:
	docker compose down -v

build:
	docker compose build sandbox-python sandbox-office sandbox-pptx sandbox-webapp
	docker compose build

logs:
	docker compose logs -f --tail=100

reset:
	docker compose down -v
	docker compose build sandbox-python sandbox-office sandbox-pptx sandbox-webapp
	docker compose up -d --build --remove-orphans

# ── Database Management ────────────────────────────────────────────────
# Run via the backend container (has all Python deps: psycopg2, redis, minio, alembic).
# Uses --no-deps so no other services are started.

DB_CMD = docker compose run --rm --no-deps --entrypoint "" backend python scripts/db_manage.py

db-check:
	@$(DB_CMD) check

db-clear:
	@echo ""
	@echo "WARNING: This will DELETE ALL DATA from PostgreSQL, Redis, and MinIO!"
	@echo "Press Ctrl+C within 5 seconds to cancel..."
	@sleep 5
	@$(DB_CMD) clear

db-setup:
	@$(DB_CMD) setup

db-reset:
	@echo ""
	@echo "WARNING: This will DELETE ALL DATA from PostgreSQL, Redis, and MinIO,"
	@echo "         then recreate schema and seed fresh data!"
	@echo "Press Ctrl+C within 5 seconds to cancel..."
	@sleep 5
	@$(DB_CMD) reset

# Print which database the running backend is actually using. Always safe.
# Equivalent to: curl http://localhost:5002/api/_db-info
db-info:
	@echo "=== /api/_db-info (live) ==="
	@curl -s http://localhost:5002/api/_db-info | python3 -m json.tool || \
		(echo "Backend not reachable on :5002 — is it up?"; exit 1)
	@echo ""
	@echo "=== check_db.py (run inside backend container) ==="
	@docker exec -e PYTHONPATH=/app zhanlu-backend python /app/scripts/check_db.py 2>&1 || \
		echo "Could not run check_db.py — is zhanlu-backend container up?"

# ── Testing ───────────────────────────────────────────────────────────

smoke:
	@echo "Running E2E smoke test..."
	@chmod +x scripts/smoke_e2e.sh
	@./scripts/smoke_e2e.sh

smoke-ui:
	@echo "Running UI E2E test (Playwright)..."
	@python3 scripts/smoke_e2e_ui.py
