"""MySQL connector URL must embed the REAL bound password — never the '***'
placeholder regression (2026-09-02 review, critical #1).

Every MySQL/MariaDB datasource query authenticates with whatever password is
in the engine URL. At one point _build_url() computed pwd_enc but returned a
URL with the literal password '***', silently breaking EVERY MySQL datasource
(access denied) while postgres/mssql/oracle connectors were fine. Credentials
always come from the bound KB row — dynamic, never a fixed database.
"""

from types import SimpleNamespace

from app.services.db.mysql import MySQLConnector


def _kb(**overrides):
    base = dict(
        username="report_user",
        password="p@ss:w ord",
        host="10.0.0.9",
        port=3306,
        database_name="erp_prod",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_url_uses_urlencoded_real_password():
    url = MySQLConnector(_kb())._build_url()
    # quote_plus: space becomes '+', '@' → %40, ':' → %3A
    assert "p%40ss%3Aw+ord" in url, url
    assert "***" not in url, f"placeholder password leaked into engine URL: {url}"


def test_build_url_defaults_and_charset():
    url = MySQLConnector(_kb(password="", port=None, database_name="", host="localhost"))._build_url()
    assert url.startswith("mysql+pymysql://report_user:@localhost:3306/")
    assert "charset=utf8mb4" in url


def test_build_url_special_chars_encoded():
    url = MySQLConnector(_kb(username="u ser", password="p/ass@rd#1"))._build_url()
    assert "u+ser" in url
    assert "p%2Fass%40rd%231" in url
    # No raw '@' or '/' separator confusion in the credential portion
    auth_part = url.split("://", 1)[1].split("@", 1)[0]
    assert "/" not in auth_part.replace("%2F", "")
