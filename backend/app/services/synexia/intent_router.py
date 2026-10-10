"""File-format intent detection.

A small, dependency-free helper that scans a user message for explicit
file-format keywords (``docx`` / ``pptx`` / ``xlsx`` / ``pdf`` / ``md``)
and returns the matched format, or ``None`` if no file-format intent is
present.

This is the single source of truth for "did the user ask for a file in
format X?" — used by:

* ``synexia/finalize.py`` — to set the right ``user_signal`` and
  pre-trigger ``run_sandbox_skill`` so the first response already
  contains a downloadable artifact.
* ``agent_prompts.py`` — to inject a routing rule into the system
  prompt for the General Assistant.
* ``skill_routing/resolver.py`` — to auto-pick the built-in creation
  skill for a requested format.
* Frontend callers that want to mirror the backend's intent decision.

CRITICAL (2026-08-31): the same format keyword can mean READ or CREATE.

* ``"read this docx, summarize it"`` → the user points at an EXISTING
  uploaded file — there is NO intent to create a docx. Before the READ
  guard below, this returned ``"docx"`` and the skill resolver auto-picked
  the docx creation skill, so the deliverable machinery fabricated a
  docx artifact (with hallucinated "warehouse data") instead of reading
  the upload.
* ``"make me a DOCX sales report"`` → a CREATE request — must still
  return ``"docx"``.

``is_file_read_request`` implements the discrimination (read verbs /
attachment references / target-format phrases), and ``detect_convert_target``
returns the TARGET format of a conversion request (``"convert this docx to
pdf"`` → ``"pdf"``, NOT ``"docx"``). Both run BEFORE any format matching.

The check is intentionally cheap (no LLM call) so it sits in the hot
path of every chat turn.  If we ever want fancier intent detection
(LLM classifier, embedding-similarity to a labeled set), it slots in
here — the public function ``detect_file_intent`` stays the same.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from app.services.harness.rule_events import observe_rule

# Recognized file formats.  Order matters: the regex tries them in this
# order, so we put the more specific multi-letter tokens first ("docx"
# before "doc", "pptx" before "ppt").  This avoids "docx" matching as
# "doc" first and returning the wrong format.
FileFormat = Literal["docx", "pptx", "xlsx", "csv", "pdf", "md", "html", "dashboard"]

# Prefix patterns.  We use a word-boundary + case-insensitive match on
# the *first 3+ letters* of each keyword so the helper still picks up
# natural-language variants like "docx file", "PDF report",
# "PowerPoint (.pptx)", "Excel workbook (xlsx)", "markdown summary".
#
# - "pptx" prefix also matches "ppt" / "pptx" / "powerpoint"
# - "xlsx" prefix also matches "xls" / "xlsx" / "excel"
# - "md"   prefix also matches "md" / "markdown"
_FORMAT_PATTERNS: tuple[tuple[FileFormat, str], ...] = (
    ("docx", r"\b(?:docx|word\s*document)\b|\.docx?\b|公文|报告文档"),
    ("pptx", r"(?<![a-zA-Z])(?:pptx|ppt|powerpoint)(?![a-zA-Z])|\.ppt\b|presentation\s*deck|slide\s*deck|pitch\s*deck|project\s*deck|演示文稿|幻灯片"),
    ("xlsx", r"\b(?:xlsx|excel\s*workbook|spreadsheet)\b|\.xls\b"),
    ("csv",  r"(?<![a-zA-Z])csv(?![a-zA-Z])|\.csv\b"),
    ("pdf",  r"\b(?:pdf)\b"),
    ("md",   r"\bmarkdown\b|\.md\b"),
    ("html", r"\b(?:html|\.html?|web\s*page|webpage)\b"),
    ("dashboard", r"\b(?:dashboard|dash\-board|kpi\s*dashboard)\b"),
)

# Compile once.  We re-use these patterns in ``detect_file_intent``.
_COMPILED: dict[FileFormat, re.Pattern[str]] = {
    fmt: re.compile(pat, re.IGNORECASE) for fmt, pat in _FORMAT_PATTERNS
}

# ---------------------------------------------------------------------------
# READ / ANALYZE guard (2026-08-31)
# ---------------------------------------------------------------------------
# A format keyword in a READ request is the OBJECT of the read action
# ("read this docx") — the user points at an EXISTING file, they are NOT
# asking to create a file in that format.  Only CREATE/CONVERT requests
# must route to the format's creation skill.  ``is_file_read_request`` is
# the single discrimination point used by:
#   * detect_file_intent          (format-intent tier of the skill resolver)
#   * normalize_deliverable_intent (goal-contract normalizer)
#   * detect_soft_intent           (soft-intent tier of the skill resolver)
# so all three deterministic layers agree.

# File-reading / analysis verbs.  Gerunds/3rd-person forms ("summarizing",
# "summarizes") deliberately do NOT match — "make a docx summarizing Q3"
# is a CREATE request.  The ZH list covers 读取/阅读/查看/总结/分析/解释 etc.
_READ_VERB_RE = re.compile(
    r"\b(?:read|summarize|summarise|analy[sz]e|explain|extract|translate|"
    r"review|parse|interpret|describe|outline|recap|digest|preview|inspect|"
    r"open|highlight|break\s+down|go\s+through|look\s+at|look\s+over|"
    r"walk\s+me\s+through|tell\s+me\s+about|tell\s+me\s+what'?s\s+in|"
    r"what'?s\s+in|what\s+is\s+in|contents?\s+of|details?\s+of)\b"
    # "view"/"show" are ONLY read verbs in phrase form ("show me the pptx",
    # "view this docx") — standalone they are nouns ("a dashboard view",
    # "make a view of sales") and must not suppress CREATE routing.
    r"|\bshow\s+me\b|\bshow\s+(?:the|this|that)\b|\bview\s+(?:the|this|that)\b"
    r"|读取|阅读|查看|打开|总结|概括|分析|解释|提取|翻译|解读|读一下|看一看|内容"
    r"|有什么|里面有什么|里有什么",
    re.IGNORECASE,
)

# "summary/overview/... OF this / the attached / the uploaded <file>" — the
# noun phrase points at an existing file, not a requested deliverable.
# Bare "the" is deliberately EXCLUDED: "a summary of the data" is a CREATE
# (make a file from data), while "a summary of this docx" is a READ.
_READ_OF_REF_RE = re.compile(
    r"\b(?:summary|overview|recap|digest|analysis|contents?|details?)\s+of\s+"
    r"(?:this|that|the\s+attached|the\s+uploaded)\b"
    r"|(?:总结|概括|分析|内容)\s*(?:一下|一下下)?\s*(?:这个|该|附件|上传的)",
    re.IGNORECASE,
)

# Explicit reference to an existing/attached file ("this docx",
# "the attached pptx", "this file", "I sent", 这个文件 / 附件 / 上传的...).
_ATTACH_REF_RE = re.compile(
    r"\b(?:this|that|the\s+attached|the\s+uploaded|the\s+file|the\s+document|"
    r"attached\s+file|uploaded\s+file|attached|uploaded|sent)\b"
    r"|这个|该文件|附件|上传的|此文件|我发的|我上传的|我附的",
    re.IGNORECASE,
)

# Target-format phrase: "in a docx", "as a pdf", "into a pptx", "to xlsx" —
# the format is the OUTPUT the user wants, so the turn is CREATE/CONVERT,
# never a read of an existing file.  Checked FIRST so "summarize this docx
# into a pdf" correctly falls through to conversion handling.
_TARGET_PHRASE_RE = re.compile(
    r"\b(?:in|as|into|to)\s+(?:a|an|the)?\s*(?:"
    r"docx?|word\s*document|pptx?|powerpoint|xlsx?|excel|pdf|markdown|html?|"
    r"report|summary|deck|presentation|dashboard|spreadsheet)\b"
    r"|(?:格式|保存为|输出为|导出为|存成|写成|做成|整理成)\s*"
    r"(?:docx?|pdf|pptx?|xlsx?|word|excel|powerpoint|markdown|html|报告|文档|总结|看板)",
    re.IGNORECASE,
)

# Conversion verbs — "convert this docx to pdf" produces a PDF, not a docx.
_CONVERT_VERB_RE = re.compile(
    r"\b(?:convert|transform|change|turn)\b"
    r"|转成|转换成|转为|转化为|改成|变为|导出为|保存为|转成|换成",
    re.IGNORECASE,
)

# Target token → canonical format (for ``detect_convert_target``).
_TARGET_TOKEN_MAP: dict[str, FileFormat] = {
    "docx": "docx", "doc": "docx", "word": "docx", "worddocument": "docx",
    "pptx": "pptx", "ppt": "pptx", "powerpoint": "pptx",
    "xlsx": "xlsx", "xls": "xlsx", "excel": "xlsx",
    "csv": "csv",
    "pdf": "pdf",
    "md": "md", "markdown": "md",
    "html": "html", "htm": "html", "webpage": "html",
    "dashboard": "dashboard",
}


@observe_rule("intent_router.is_file_read_request", forced="suppress_creation")
def is_file_read_request(text: Optional[str]) -> bool:
    """True when the message asks to READ / ANALYZE an existing file.

    Used as a guard by every deterministic intent layer: a format keyword
    in a READ request is the object of the read action ("read this docx"),
    not a request to create a file in that format.  Creation phrasing is
    preserved: "make a docx summarizing Q3" has no READ verb ("summarizing"
    does not match ``\\bsummarize\\b``) so it still routes to the docx skill.
    """
    if not text:
        return False
    # Target-format phrase ("in a docx", "as a pdf") means the format is the
    # OUTPUT — never suppress a CREATE/CONVERT for those.
    if _TARGET_PHRASE_RE.search(text):
        return False
    if _READ_OF_REF_RE.search(text):
        return True
    if _READ_VERB_RE.search(text):
        if any(_COMPILED[f].search(text) for f in _COMPILED):
            return True
        if _ATTACH_REF_RE.search(text):
            return True
    return False


# ── NEGATION guard (2026-09-18) ────────────────────────────────────────────
# A deliverable mention can FORBID, not just request. Live case: the user asked
# "本周销售情况…直接输出，不要输出 文档" — the request names 文档 (document) and
# forbids it. Every detector here is keyword-positive, so the prohibition read
# as a request, the file-turn guard force-nudged create_artifact near the
# iteration cap, and the turn ended with a docx the user had explicitly refused
# (the model obeyed the platform and then apologised for it). Same family as the
# READ guard above: a format noun inside a non-create speech act is not intent.
#
# The patterns bind the negation DIRECTLY to the deliverable noun —
#   cue (optional creation verb) (optional filler) (optional measure word) noun
# — so a negation of something else in the same sentence ("不要包括文档中未列出的列",
# "don't include columns the document omits") does NOT suppress a genuine request.
_DELIVERABLE_NOUNS = (
    r"(?:文档|文件|表格文件|电子表格|幻灯片|演示文稿|公文|附件"
    r"|docx?|document|word|excel|xlsx|xls|pdf|pptx?|powerpoint"
    r"|slide\s*deck|spreadsheet|attachment|file)"
)
_CREATE_VERB = (
    r"(?:输出|生成|创建|导出|做|写|提供|给|发|要|做成|转成|转为|转换为|另存为|保存为"
    r"|create|generate|make|produce|export|write|build|send|give)"
)
_NEGATION_CUE = (
    r"(?:不要|不用|不需要|无需|不必|别|甭|no\s+need|not\s+need"
    r"|don'?t|do\s+not|doesn'?t|does\s+not|won'?t|will\s+not|without|avoid|never|no|not)"
)
_DELIVERABLE_NEGATED_RE = re.compile(
    rf"(?:{_NEGATION_CUE})"
    rf"\s*(?:再|直接)?\s*"
    rf"(?:给我|带给|帮你|发我)?\s*"
    rf"(?:{_CREATE_VERB})?\s*"
    rf"(?:to|for|of)?\s*"
    rf"(?:一份|一个|一张|a|an|the|any)?\s*"
    rf"{_DELIVERABLE_NOUNS}",
    re.IGNORECASE,
)


def is_deliverable_negated(text: Optional[str]) -> bool:
    """True when the message explicitly REFUSES a file/document deliverable.

    Only fires when the negation binds to a deliverable noun (see
    ``_DELIVERABLE_NEGATED_RE``), so it never suppresses a request that merely
    contains a negative word about something else. Used by every deterministic
    intent layer as a hard constraint: an explicit user instruction not to
    produce a file outranks any harness-level nudge to produce one.
    """
    if not text:
        return False
    return bool(_DELIVERABLE_NEGATED_RE.search(text))


# ── Visualisation deliverable ───────────────────────────────────────────
# A chart the user asked to SEE is a deliverable in the same sense a file is,
# but it has no file format — so `detect_file_intent` never fires and no guard
# ever checked it. Observed live: "本月采购订单分析，用图表展示" parsed as
# task_kind=create_artifact, yet the plan produced an xlsx (+ its preview) and
# the answer claimed a chart existed. Nothing enforced the chart.
#
# These terms mirror the vocabulary declared by the `inline-chart` skill, so
# the detector and the skill stay consistent: whichever phrasing matches the
# skill's semantic search also arms the guard.
_VISUAL_CHART_TERMS = (
    r"(?:图表|可视化|走势图|趋势图|柱状图|柱形图|条形图|折线图|曲线图"
    r"|饼图|饼状图|环形图|圆环图|散点图|面积图|直方图|分布图"
    r"|charts?|graphs?|plots?|visuali[sz]ation|visuali[sz]e|data\s*viz"
    r"|bar\s*chart|column\s*chart|line\s*chart|pie\s*chart"
    r"|donut|scatter\s*plot|histogram|heat\s*map)"
)
_VISUAL_INTENT_RE = re.compile(_VISUAL_CHART_TERMS, re.IGNORECASE)
# "不要图表 / don't plot this" — a negation bound to a CHART term. The file
# negation regex above only knows document nouns, so it cannot see this, and
# without it the guard would force a chart the user explicitly refused.
_VISUAL_NEGATED_RE = re.compile(
    rf"(?:{_NEGATION_CUE})"
    rf"\s*(?:再|直接)?\s*"
    rf"(?:给我|帮我|发我)?\s*"
    rf"(?:{_CREATE_VERB})?\s*"
    rf"(?:一个|一张|一份|a|an|the|any)?\s*"
    rf"{_VISUAL_CHART_TERMS}",
    re.IGNORECASE,
)


@observe_rule("intent_router.detect_visual_intent")
def detect_visual_intent(text: Optional[str]) -> bool:
    """True when the user asked to SEE the data as a chart / graph / plot.

    This is an enforcement signal, not a routing decision: it never selects a
    tool or a skill, and it never authors the answer. It only tells the turn
    guard that a rendered chart is part of the deliverable, so a turn that
    produced no visual artifact cannot close on prose that describes numbers.

    Examples
    --------
    >>> detect_visual_intent("本月采购订单分析，用图表展示")
    True
    >>> detect_visual_intent("画个柱状图")
    True
    >>> detect_visual_intent("plot weekly revenue as a bar chart")
    True
    >>> detect_visual_intent("summarize this table as text")
    False
    >>> detect_visual_intent("不要图表，直接把数字写在回复里")
    False
    """
    if not text:
        return False
    if _VISUAL_NEGATED_RE.search(text):
        return False
    return bool(_VISUAL_INTENT_RE.search(text))


def detect_convert_target(text: Optional[str]) -> Optional[FileFormat]:
    """Return the TARGET format of a conversion request, or ``None``.

    ``"convert this docx to pdf"`` → ``"pdf"`` (the pre-existing code
    returned ``"docx"`` — the FIRST matched format — so the resolver
    picked the docx skill for a convert-to-pdf request).  Returns ``None``
    when the request is not a conversion, or the target token is not a
    recognized deliverable format (let the LLM handle exotic targets).
    """
    if not text or not _CONVERT_VERB_RE.search(text):
        return None
    low = text.lower()
    m = re.search(r"\b(?:to|into)\s+(?:a|an|the)?\s*([a-z][a-z0-9.]{1,20})\b", low)
    target: Optional[str] = m.group(1) if m else None
    if target is None:
        m = re.search(
            r"(?:转成|转换成|转为|转化为|改成|变为|导出为|保存为)\s*"
            r"([\u4e00-\u9fff\w.]{1,12})",
            low,
        )
        if m:
            target = m.group(1)
    if not target:
        return None
    norm = target.rstrip(".").strip().replace(" ", "")
    if norm in _TARGET_TOKEN_MAP:
        return _TARGET_TOKEN_MAP[norm]
    return None


@observe_rule("intent_router.detect_file_intent")
def detect_file_intent(text: Optional[str]) -> Optional[FileFormat]:
    """Return the file format the user asked for, or ``None``.

    Examples
    --------
    >>> detect_file_intent("make me a DOCX sales report")
    'docx'
    >>> detect_file_intent("can I have it as a PowerPoint?")
    'pptx'
    >>> detect_file_intent("export to xlsx please")
    'xlsx'
    >>> detect_file_intent("send me the markdown summary")
    'md'
    >>> detect_file_intent("hello, how are you?")
    None
    >>> detect_file_intent("")
    None
    >>> detect_file_intent(None)
    None
    """
    if not text:
        return None
    # ── READ / ANALYZE guard (2026-08-31) ───────────────────────────
    # "read this docx" / "summarize the attached pptx" point at an EXISTING
    # file: the format keyword is the object of the read, not a deliverable
    # request.  Return None so the skill resolver does NOT auto-pick the
    # format's creation skill (which fabricated an artifact instead of
    # reading the upload).  CREATE phrasing ("make me a DOCX report") has
    # no READ verb and falls through unchanged.
    if is_file_read_request(text):
        return None
    # ── NEGATION guard (2026-09-18) ─────────────────────────────────
    # "不要输出文档" / "no document needed" REFUSES the deliverable. Without
    # this, the positive keyword scan below reads the prohibition as a
    # request and the file/dashboard/pptx turn guards force a file the user
    # explicitly refused.
    if is_deliverable_negated(text):
        return None
    # ── CONVERT: return the TARGET format ───────────────────────────
    # "convert this docx to pdf" produces a PDF — the pre-existing code
    # returned the FIRST matched format ("docx") and routed the wrong
    # creation skill.
    _conv = detect_convert_target(text)
    if _conv is not None:
        return _conv
    # Goal-Contract mode: the typo-tolerant normalizer is the single source
    # of truth for file-format intent (dashboard-first priority, EN+ZH,
    # Dashbord-class typos). Fall back to the legacy patterns only when it
    # returns None so edge phrasing never regresses.
    try:
        from app.config import settings

        if getattr(settings, "GOAL_CONTRACT_ENABLED", False):
            from app.services.goal_contract import normalize_deliverable_intent

            fmt = normalize_deliverable_intent(text)
            if fmt in {"docx", "pptx", "xlsx", "pdf", "md", "html"}:
                return fmt
            if fmt == "dashboard":
                # DASHBOARD_FUZZY_MATCH_ENABLED is authoritative for
                # typo-tolerant dashboard detection ("Dashbord" etc.). When
                # the flag is off, fall through to the legacy patterns —
                # an EXACT "dashboard" is still matched by the compiled
                # regex below, but typo/Chinese-normalizer matches are not
                # (the flag exists precisely to gate those).
                try:
                    if getattr(settings, "DASHBOARD_FUZZY_MATCH_ENABLED", False):
                        return fmt
                except Exception:
                    return fmt
    except Exception:
        pass
    # Iterate in priority order — first hit wins.
    for fmt, pattern in _COMPILED.items():
        if pattern.search(text):
            return fmt
    # Flag-gated fuzzy "dashboard" typo fallback ("Dashbord" / "dash-board"
    # etc.) so a typo still reaches the dashboard machinery. Lazy import keeps
    # this module import-free at load time (the helper itself lazily reads the
    # DASHBOARD_FUZZY_MATCH_ENABLED flag and no-ops when it is off).
    try:
        from app.services.dashboard_turn_guard import fuzzy_dashboard_request

        if fuzzy_dashboard_request(text):
            return "dashboard"
    except Exception:
        pass
    return None


# Map format → "export_<fmt>" user_signal string used by
# ``synexia/user_signal.py`` and ``MessageBubble.jsx``.
EXPORT_SIGNAL_BY_FORMAT: dict[FileFormat, str] = {
    "docx": "export_docx",
    "pptx": "export_pptx",
    "xlsx": "export_xlsx",
    "pdf":  "export_pdf",
    "md":   "export_md",
    "html": "export_html",
    "dashboard": "export_dashboard",
}


def user_signal_for_format(fmt: FileFormat) -> str:
    """Return the canonical user_signal string for a given file format."""
    return EXPORT_SIGNAL_BY_FORMAT[fmt]


# ---------------------------------------------------------------------------
# Deck-edit intent detection
# ---------------------------------------------------------------------------
#
# Mirrors ``detect_file_intent``: cheap, dependency-free, regex-only.  Maps a
# user message to one of the six deck-edit tools, or ``None`` when the message
# is not an edit request.  Regeneration phrasing ("redo", "from scratch",
# "regenerate") short-circuits to ``None`` so the caller keeps the full
# regeneration path instead of editing an existing artifact.

# Recognized deck-edit tools.  Order matters: more specific patterns win.
DeckEditTool = Literal[
    "edit_slide", "add_slide", "restyle_deck", "update_chart",
    "remove_slide", "reorder_slide",
]

# Regeneration phrases — these mean "throw the old one away and rebuild", NOT
# "edit the existing deck".  Checked before the edit patterns.
_DECK_EDIT_REGENERATE_RE = re.compile(
    r"\b(?:regenerate|re-?generate|redo|re-?do|from\s+scratch|re-?make|remake)\b"
    r"|重新生成|重做|重来|从头生成|重新做",
    re.IGNORECASE,
)

# Slide index fragment: digits or common Chinese numerals (一二三四五六七八九十).
# e.g. "第3页", "第三页", "第一页".
_SLIDE_IDX = r"[0-9一二三四五六七八九十百]*"

_DECK_EDIT_INTENT_PATTERNS: tuple[tuple[DeckEditTool, str], ...] = (
    (
        "remove_slide",
        r"\b(?:remove|delete)\s+(?:the\s+)?(?:slide|page)\b"
        r"|删除第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|删掉第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|移除第?\s*" + _SLIDE_IDX + r"\s*[页張张]",
    ),
    (
        "reorder_slide",
        r"\b(?:reorder|re-?order|rearrange)\b|调整顺序|调换顺序"
        r"|移动第?\s*" + _SLIDE_IDX + r"\s*[页張张]",
    ),
    (
        "add_slide",
        r"\b(?:add|insert)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:slide|page)\b"
        r"|添加一页|新增一页|加一页|加个页面|增加一页",
    ),
    (
        "update_chart",
        r"\b(?:update|change|modify|edit)\s+(?:the\s+)?chart\b"
        r"|修改图表|更新图表|改图表|改下图表|调整图表",
    ),
    (
        "restyle_deck",
        r"\b(?:change|switch|swap)\s+(?:the\s+)?(?:to\s+)?(?:a\s+|an\s+)?"
        r"(?:different\s+)?(?:theme|style|color\s*scheme)\b"
        r"|换个主题|换主题|改主题|更换主题|换个风格|改风格|换风格",
    ),
    (
        "edit_slide",
        r"\b(?:edit|update|change|modify|revise|rewrite)\s+(?:the\s+)?(?:slide|page)\b"
        r"|修改第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|编辑第?\s*" + _SLIDE_IDX + r"\s*[页張张]"
        r"|改第?\s*" + _SLIDE_IDX + r"\s*[页張张]",
    ),
)

_DECK_EDIT_COMPILED: dict[DeckEditTool, re.Pattern[str]] = {
    tool: re.compile(pat, re.IGNORECASE)
    for tool, pat in _DECK_EDIT_INTENT_PATTERNS
}


@observe_rule("intent_router.detect_deck_edit_intent", forced="deck_edit")
def detect_deck_edit_intent(text: Optional[str]) -> Optional[str]:
    """Return the deck-edit tool name for a message, or ``None``.

    Regeneration phrasing (``regenerate`` / ``redo`` / ``from scratch`` /
    ``重新生成`` / ``重做`` ...) short-circuits to ``None`` so the caller
    keeps the full regeneration path.
    """
    if not text:
        return None
    if _DECK_EDIT_REGENERATE_RE.search(text):
        return None
    for tool, pattern in _DECK_EDIT_COMPILED.items():
        if pattern.search(text):
            return tool
    return None
