"""Provenance Guard: multi-signal AI-attribution API for creative text."""
import json
import os
import re
import sqlite3
import statistics
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

DB_PATH = os.environ.get("PG_DB", os.path.join(os.path.dirname(__file__), "provenance.db"))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")  # llama-4-scout was retired on Groq
W_LLM, W_STYLE = 0.65, 0.35          # planning.md: combined score weights
AI_THRESHOLD, HUMAN_THRESHOLD = 0.70, 0.35
APPEAL_MAX_AI = 0.80                 # AI verdicts above this are not self-serve appealable
SHORT_TEXT_WORDS = 40
UNSURE_CAP = 0.69                    # cap when evidence is weak (short text / LLM down)

app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

LABELS = {
    "AI_generated_or_assisted": (
        "Likely AI-generated or AI-assisted. Our automated checks found strong signs that this "
        "work was produced with AI. Automated checks can be wrong. If you wrote this yourself, "
        "the creator can appeal and a person will review it."
    ),
    "Human_made": (
        "Likely human-written. Our automated checks found no strong signs of AI generation. "
        "This is an estimate, not a guarantee."
    ),
    "No_classification": (
        "Origin unclear. Our automated checks could not tell whether this was written by a person "
        "or with AI. No conclusion is being drawn about this work. The creator can request a review."
    ),
    "under_review": (
        "Under review. The creator has contested this label and a person is reviewing it."
    ),
}


# ---------------------------------------------------------------- storage
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS content (
                content_id TEXT PRIMARY KEY, creator_id TEXT, text TEXT,
                attribution TEXT, confidence REAL, p_ai REAL,
                llm_score REAL, stylometric_score REAL, signals_used TEXT,
                status TEXT, appeal_reasoning TEXT, appeal_timestamp TEXT,
                created_at TEXT);
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, event TEXT,
                content_id TEXT, creator_id TEXT, attribution TEXT, confidence REAL,
                llm_score REAL, stylometric_score REAL, signals_used TEXT,
                status TEXT, appeal_reasoning TEXT);
            """
        )


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_event(c, event, row, status, appeal_reasoning=None):
    c.execute(
        "INSERT INTO audit_log (timestamp,event,content_id,creator_id,attribution,confidence,"
        "llm_score,stylometric_score,signals_used,status,appeal_reasoning) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now(), event, row["content_id"], row["creator_id"], row["attribution"], row["confidence"],
         row["llm_score"], row["stylometric_score"], row["signals_used"], status, appeal_reasoning),
    )


def get_log(limit=50):
    with db() as c:
        rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["signals_used"] = json.loads(d["signals_used"]) if d["signals_used"] else []
        d["appeal_filed"] = d["event"] == "appeal"
        out.append(d)
    return out


# ---------------------------------------------------------------- signal 1: LLM
_PROMPT = (
    "You are an expert at telling human-written text from AI-generated text. Assess the text "
    "below for: transition density ('Furthermore', 'It is important to note'), uniform pacing, "
    "neutral over-balanced tone, generic phrasing, versus human idiosyncrasy, irregular "
    "emotion, specific personal detail and non-linear logic. Reply with ONLY a JSON object: "
    '{"ai_probability": <float 0.0-1.0>, "reason": "<one short sentence>"}. '
    "0.0 = certainly human, 1.0 = certainly AI.\n\nTEXT:\n\"\"\"\n%s\n\"\"\""
)


def llm_signal(text):
    """Signal 1 (semantic). Returns float in [0,1], or None if the LLM is unavailable."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    try:
        from groq import Groq

        resp = Groq(api_key=key).chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": _PROMPT % text[:6000]}],
            temperature=0,
            max_tokens=1000,
        )
        raw = resp.choices[0].message.content
        m = re.search(r'"ai_probability"\s*:\s*([0-9]*\.?[0-9]+)', raw)
        if not m:
            return None
        return max(0.0, min(1.0, float(m.group(1))))
    except Exception as exc:  # network, auth, rate limit: degrade gracefully
        app.logger.warning("llm_signal failed: %s", exc)
        return None


# ---------------------------------------------------------------- signal 2: stylometry
def split_sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if len(s.split()) > 0]


def stylometric_signal(text):
    """Signal 2 (structural). Sentence-length variation -> [0,1]; 1.0 = uniform (AI-like)."""
    lengths = [len(s.split()) for s in split_sentences(text)]
    if len(lengths) < 3:
        return 0.5  # not enough sentences to measure burstiness: no information
    cv = statistics.pstdev(lengths) / statistics.mean(lengths)
    return round(max(0.0, min(1.0, 1 - cv / 0.8)), 4)


# ---------------------------------------------------------------- scoring + label
def combine(s1, s2, n_words):
    """Returns (p_ai, signals_used). Weights/caps are defined in planning.md."""
    if s1 is None:
        p, used = s2, ["stylometric"]
        p = min(p, UNSURE_CAP)
    else:
        p, used = W_LLM * s1 + W_STYLE * s2, ["llm", "stylometric"]
    if n_words < SHORT_TEXT_WORDS:
        p = min(p, UNSURE_CAP)
    return round(p, 4), used


def classify(p_ai):
    if p_ai >= AI_THRESHOLD:
        return "AI_generated_or_assisted", p_ai
    if p_ai <= HUMAN_THRESHOLD:
        return "Human_made", 1 - p_ai
    return "No_classification", max(p_ai, 1 - p_ai)  # 0.5 = coin flip; never above 0.65


def make_label(attribution, status="classified"):
    return LABELS["under_review"] if status == "under_review" else LABELS[attribution]


# ---------------------------------------------------------------- routes
@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text, creator_id = data.get("text"), data.get("creator_id")
    if not isinstance(text, str) or not text.strip() or not creator_id:
        return jsonify(error="'text' and 'creator_id' are required"), 400
    if len(text) > 20000:
        return jsonify(error="text too long (max 20000 chars)"), 413

    s1 = llm_signal(text)
    s2 = stylometric_signal(text)
    p_ai, used = combine(s1, s2, len(text.split()))
    attribution, confidence = classify(p_ai)
    row = dict(content_id=str(uuid.uuid4()), creator_id=creator_id, attribution=attribution,
               confidence=round(confidence, 4), llm_score=s1, stylometric_score=s2,
               signals_used=json.dumps(used))
    with db() as c:
        c.execute(
            "INSERT INTO content VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["content_id"], creator_id, text, attribution, row["confidence"], p_ai, s1, s2,
             row["signals_used"], "classified", None, None, now()),
        )
        log_event(c, "submission", row, "classified")
    return jsonify(
        content_id=row["content_id"], attribution=attribution, confidence=row["confidence"],
        p_ai=p_ai, signals={"llm": s1, "stylometric": s2}, signals_used=used,
        status="classified", label=make_label(attribution),
    )


@app.post("/appeal")
def appeal():
    data = request.get_json(silent=True) or {}
    cid, reasoning = data.get("content_id"), (data.get("creator_reasoning") or "").strip()
    creator_id = data.get("creator_id")
    if not cid or not reasoning:
        return jsonify(error="'content_id' and 'creator_reasoning' are required"), 400
    with db() as c:
        row = c.execute("SELECT * FROM content WHERE content_id=?", (cid,)).fetchone()
        if not row:
            return jsonify(error="unknown content_id"), 404
        if creator_id and creator_id != row["creator_id"]:
            return jsonify(error="only the original creator can appeal"), 403
        if row["status"] == "under_review":
            return jsonify(error="an appeal is already under review"), 409
        if row["attribution"] == "Human_made" or (
            row["attribution"] == "AI_generated_or_assisted" and row["p_ai"] > APPEAL_MAX_AI
        ):
            return jsonify(error="this classification is not eligible for self-serve appeal"), 400
        c.execute("UPDATE content SET status='under_review', appeal_reasoning=?, appeal_timestamp=? "
                  "WHERE content_id=?", (reasoning, now(), cid))
        log_event(c, "appeal", dict(row), "under_review", reasoning)
    return jsonify(content_id=cid, status="under_review", label=LABELS["under_review"],
                   original_attribution=row["attribution"], original_confidence=row["confidence"],
                   message="Appeal received and logged alongside the original decision.")


@app.get("/appeals")
def appeals_queue():
    with db() as c:
        rows = c.execute("SELECT * FROM content WHERE status='under_review' "
                         "ORDER BY appeal_timestamp").fetchall()
    return jsonify(queue=[dict(
        content_id=r["content_id"], creator_id=r["creator_id"],
        original_attribution=r["attribution"], original_confidence=r["confidence"],
        llm_score=r["llm_score"], stylometric_score=r["stylometric_score"],
        text_excerpt=r["text"][:300], creator_reasoning=r["appeal_reasoning"],
        appeal_timestamp=r["appeal_timestamp"]) for r in rows])


@app.get("/log")
def log():
    return jsonify(entries=get_log())


@app.errorhandler(429)
def too_many(e):
    return jsonify(error="rate limit exceeded", detail=str(e.description)), 429


init_db()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5001)), debug=False)
