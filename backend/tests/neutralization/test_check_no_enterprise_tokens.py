import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_SCRIPT = "scripts/check_no_enterprise_tokens.py"


def _find_repo() -> Path:
    """Locate the repo root that holds scripts/check_no_enterprise_tokens.py.

    Works both on the host (…/zhanlu_n8n_main_version) and inside the backend
    container (where ./backend is mounted at /app and ./scripts at /app/scripts).
    """
    here = Path(__file__).resolve()
    for cand in (here, *here.parents):
        if (cand / REPO_SCRIPT).exists():
            return cand
    raise RuntimeError(f"could not locate repo root containing {REPO_SCRIPT}")


REPO = _find_repo()
SCRIPT = REPO / REPO_SCRIPT


def _load():
    spec = importlib.util.spec_from_file_location("checker", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_token_matching_rules():
    m = _load()
    assert m.token_matches("ecisco", "Ecisco is here")             # word-boundary, case-insensitive
    assert m.token_matches("ecisco_bi", "the ECISCO_BI agent")     # compound token
    assert not m.token_matches("ecisco", "precisco")               # no boundary -> no match
    assert not m.token_matches("ecisco", "ECISCO_BI")              # '_' is a word char; the
    #                                                                ecisco_bi token catches it
    assert m.token_matches("c5c9-daily-sales", "x c5c9-daily-sales y")  # punctuated -> substring
    assert m.token_matches("惠州", "基地在惠州")                     # CJK -> substring


def test_excluded_trees_are_skipped():
    m = _load()
    for p in ("NL2SQL/DB-GPT-main/a.py", "n8n-master/b.ts", "node_modules/c.js",
              "superpowers-main/d.md", "agent_qa/e.md"):
        assert m.is_excluded(REPO / p), p


def test_binary_files_are_skipped():
    m = _load()
    assert m.is_binary_name(Path("x/font.woff2"))
    assert m.is_binary_name(Path("x/app.min.js"))


def test_checker_exits_nonzero_on_repo_with_hits(tmp_path):
    """End-to-end: a scratch tree with a planted token must fail."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "enterprise_token_denylist.txt").write_text("ecisco\n")
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "leak.py").write_text("x = 'ecisco'\n")
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--quiet"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "ecisco" in r.stdout
