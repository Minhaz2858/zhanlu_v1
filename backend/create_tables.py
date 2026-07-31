"""Create missing refresh_tokens and revoked_tokens tables."""
import psycopg2

conn = psycopg2.connect(
    host="postgres", port=5432, dbname="zhanlu",
    user="zhanlu", password="zhanlu123",
)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id              VARCHAR(36) PRIMARY KEY,
    created_date    TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_date    TIMESTAMP NOT NULL DEFAULT NOW(),
    created_by_id   VARCHAR(36),
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    org_id          VARCHAR(36) NOT NULL DEFAULT 'default-org',
    app_id          VARCHAR(36) NOT NULL DEFAULT 'default-app',
    user_id         VARCHAR(36) NOT NULL REFERENCES users(id),
    token_hash      VARCHAR(64) NOT NULL UNIQUE,
    expires_at      TIMESTAMP NOT NULL,
    used            BOOLEAN NOT NULL DEFAULT FALSE
);
""")
print("refresh_tokens created")

for idx_name, idx_col in [
    ("ix_refresh_tokens_user_id", "user_id"),
    ("ix_refresh_tokens_token_hash", "token_hash"),
    ("ix_refresh_tokens_org_id", "org_id"),
    ("ix_refresh_tokens_app_id", "app_id"),
]:
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {idx_name} ON refresh_tokens({idx_col})"
    )
print("refresh_tokens indexes created")

cur.execute("""
CREATE TABLE IF NOT EXISTS revoked_tokens (
    id              VARCHAR(36) PRIMARY KEY,
    created_date    TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_date    TIMESTAMP NOT NULL DEFAULT NOW(),
    created_by_id   VARCHAR(36),
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    org_id          VARCHAR(36) NOT NULL DEFAULT 'default-org',
    app_id          VARCHAR(36) NOT NULL DEFAULT 'default-app',
    jti             VARCHAR(36) NOT NULL UNIQUE,
    user_id         VARCHAR(36) NOT NULL REFERENCES users(id),
    expires_at      TIMESTAMP NOT NULL
);
""")
print("revoked_tokens created")

for idx_name, idx_col in [
    ("ix_revoked_tokens_jti", "jti"),
    ("ix_revoked_tokens_user_id", "user_id"),
    ("ix_revoked_tokens_org_id", "org_id"),
    ("ix_revoked_tokens_app_id", "app_id"),
]:
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {idx_name} ON revoked_tokens({idx_col})"
    )
print("revoked_tokens indexes created")

conn.close()
print("DONE")
