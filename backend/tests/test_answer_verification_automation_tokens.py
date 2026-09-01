"""Stoplist extension for automation-task metadata tokens.

Regression guard for the live trace ("Daily Sales Data Sync" run, 2026-08-20):
the dimension-coverage detector flagged `markdown`, `outcome`, and `running`
from the automation prompt template as missing dimensions, issuing a nudge
that caused the model to re-emit the entire report (duplicating the response).

These tokens describe the TASK or its DELIVERY, not a data dimension the
results must enumerate. They must be filtered out by the stoplist pipeline
before the coverage check.

Verifies:
- Stoplist expansions (ASPECT/VERB/FORMAT/AUTOMATION stopwords catch the tokens
  that appear in the automation prompt template).
- The -ing normalization fix strips "running" → "run" so it hits _VERB_STOPWORDS.
- The _AUTOMATION_STOPWORDS set covers automation-context tokens that don't
  fit the other stopword categories (e.g. "outcome", "incremental").
"""
import pytest

from app.services import answer_verification as av


# ── The exact bug scenario: "Daily Sales Data Sync" automation run ────────


def test_daily_sales_data_sync_automation_prompt_no_phantom_flags():
    """Reproduce the exact bug: the automation prompt template embeds tokens
    like 'markdown', 'outcome', 'running', 'incremental', 'anomaly', 'alerts'
    that previously triggered phantom missing-dimension flags."""
    user_prompt = (
        "Run Automation Task:\n"
        "- Name: Daily Sales Data Sync\n"
        "- Type: data_sync\n"
        "- Output format: Web page (html)\n"
        "- Project: Ecisco BI\n"
        "- Description: Sync ERP sales data to the business database daily with "
        "incremental updates and anomaly alerts\n\n"
        "Format: the deliverable is an HTML report. Use markdown tables where "
        "appropriate. The outcome of this run should be a successful sync. "
        "Track running totals and watch for anomalies."
    )
    results = [{
        "tool": "execute_query",
        "columns": ["FNAME", "shipment_date", "FAMOUNT"],
        "rows": [
            {"FNAME": "PVC", "shipment_date": "2026-08-20", "FAMOUNT": 1500},
            {"FNAME": "C5", "shipment_date": "2026-08-19", "FAMOUNT": 4200},
        ],
    }]
    missing = av._detect_dimension_coverage(
        user_prompt, results,
        "ERP sales data report: total revenue ¥309.6M across 188 order lines.",
    )
    # No phantom flags for markdown / outcome / running / incremental /
    # anomaly / alerts / sync / scheduled / etc. Real data dimensions in
    # the payload (FNAME, shipment_date, FAMOUNT) cover anything that
    # actually IS a data dimension.
    assert missing == []


# ── Individual automation-context tokens ──────────────────────────────────


@pytest.mark.parametrize("token", [
    "markdown",      # _FORMAT_STOPWORDS
    "web",           # _FORMAT_STOPWORDS
    "page",          # _FORMAT_STOPWORDS
    "artifact",      # _FORMAT_STOPWORDS
    "deliverable",   # _FORMAT_STOPWORDS
    "running",       # _VERB_STOPWORDS via -ing normalization
    "syncing",       # _VERB_STOPWORDS via -ing normalization
    "sync",          # _AUTOMATION_STOPWORDS
    "scheduled",     # _VERB_STOPWORDS
    "automated",     # _VERB_STOPWORDS
    "outcome",       # _ASPECT_STOPWORDS + _AUTOMATION_STOPWORDS
    "incremental",   # _AUTOMATION_STOPWORDS
    "anomaly",       # _AUTOMATION_STOPWORDS
    "anomalies",     # _AUTOMATION_STOPWORDS
    "alerts",        # _AUTOMATION_STOPWORDS
    "alert",         # _AUTOMATION_STOPWORDS
])
def test_automation_token_filtered_out(token):
    """Each automation metadata token must NOT be flagged as a missing dim
    even when it's absent from the payload corpus."""
    results = [{"tool": "execute_query", "columns": ["amount"],
                "rows": [{"amount": 100}]}]
    missing = av._detect_dimension_coverage(
        f"create a report about {token}", results,
        "Here is the report.",
    )
    assert token not in missing, (
        f"automation metadata token {token!r} must be filtered out by the "
        f"stoplist pipeline; got missing={missing}"
    )


# ── -ing normalization ────────────────────────────────────────────────────


@pytest.mark.parametrize("verb,expected", [
    ("running", "run"),     # doubled consonant undoes: runn → run
    ("syncing", "sync"),    # no doubling, stem preserved
    # Note: silent-e before -ing (automating → automate, scheduling → schedule)
    # is NOT handled by this minimal normalizer; we only fix the doubled-consonant
    # pattern which is what the "Daily Sales Data Sync" bug needed.
])
def test_ing_suffix_stripped(verb, expected):
    """_normalize_token strips -ing suffix so inflected verb forms hit
    _VERB_STOPWORDS. Doubled-final-consonant pattern is undone (runn → run)
    so the stem matches the dictionary entry."""
    assert av._normalize_token(verb) == expected


@pytest.mark.parametrize("short_tok", ["king", "ring", "ping", "wing"])
def test_short_ing_tokens_not_stripped(short_tok):
    """Words of length <= 5 with -ing must NOT be stripped (king → k is
    too aggressive)."""
    assert av._normalize_token(short_tok) == short_tok


def test_ies_normalization_preserved():
    """Existing -ies → -y normalization must still work after the -ing fix."""
    assert av._normalize_token("categories") == "category"
    assert av._normalize_token("queries") == "query"


def test_plural_normalization_preserved():
    """Existing -s stripping must still work."""
    assert av._normalize_token("tables") == "table"
    assert av._normalize_token("runs") == "run"
    # -ss words preserved (status, class, business)
    assert av._normalize_token("class") == "class"
    assert av._normalize_token("business") == "business"


# ── Real data dimensions still flagged ─────────────────────────────────────


def test_real_dimension_still_flagged_when_absent():
    """Stoplist expansions must NOT over-suppress: a real data dimension
    like 'region' is still flagged when absent from the payload."""
    results = [{"tool": "execute_query", "columns": ["sales_amount"],
                "rows": [{"sales_amount": 3200}]}]
    missing = av._detect_dimension_coverage(
        "show me sales by region", results, "Total sales: 3200.",
    )
    assert missing == ["region"]


# ── Automation task wrapper that includes the new stopword metadata ──────


def test_automation_task_prompt_no_dims():
    """The full automation wrapper prompt with all the metadata fields
    must yield zero phantom flags when the payload contains real data."""
    user_prompt = (
        "Type: data_sync\n"
        "Output format: Web page (html)\n"
        "Description: Sync ERP sales data daily with incremental updates and "
        "anomaly alerts.\n"
        "Format: deliverable is an HTML report. Track running outcomes.\n"
    )
    results = [{
        "tool": "execute_query",
        "columns": ["customer_name", "shipment_grade", "FAMOUNT", "sale"],
        "rows": [
            {"customer_name": "中海壳牌石油化工有限公司", "shipment_grade": "A",
             "FAMOUNT": 60000, "sale": 309562191},
        ],
    }]
    missing = av._detect_dimension_coverage(
        user_prompt, results,
        "Sales report: Revenue ¥309.6M across 188 sales order lines from 83 sales "
        "customers and 22 materials.",
    )
    assert missing == []