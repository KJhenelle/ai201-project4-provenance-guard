# Provenance Guard

A Flask backend that any creative-sharing platform could plug into. It classifies submitted text as AI-generated, human-written or uncertain, scores its confidence, returns a plain-language transparency label, lets creators appeal, and records every decision in a structured audit log. Design lives in [planning.md](planning.md).

## Demo

Portfolio walkthrough video: [demo.mp4](demo.mp4) (compressed to 960x540 to fit GitHub's file-size limit). It shows a submission, the transparency label, an appeal, rate limiting and the audit log.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "GROQ_API_KEY=your_key" > .env        # never committed
python app.py                              # http://localhost:5001 (PORT env var to change)
python test_api.py                         # end-to-end tests, LLM stubbed
python test_signals.py                     # scores the 4 calibration samples
python demo.py                             # generates audit-log entries + one appeal
```
Port 5001 is the default because macOS AirPlay occupies 5000.

| Endpoint | Purpose |
|---|---|
| `POST /submit` `{text, creator_id}` | Classify. Returns `content_id, attribution, confidence, p_ai, signals, status, label`. Rate-limited. |
| `POST /appeal` `{content_id, creator_reasoning, creator_id?}` | Contest a classification; sets status `under_review`. |
| `GET /appeals` | Reviewer queue: original verdict, both scores, excerpt, reasoning. |
| `GET /log` | Most recent audit-log events. |

## Architecture overview

A submission hits `POST /submit`; Flask-Limiter checks the rate limit, then the payload is validated and a `content_id` (UUID) is created. The raw text goes to **Signal 1** (Groq LLM, semantic) and **Signal 2** (stylometry, structural). `combine()` merges the two scores into `P(AI)`, applying the weights and safety caps. `classify()` maps `P(AI)` to one of three attributions plus a confidence, `make_label()` picks the label text, and the record plus an audit-log event are written to SQLite before the JSON response is returned. An appeal looks up the stored record, checks eligibility, sets status to `under_review`, stores the creator's reasoning next to the original verdict and scores, and appends an `appeal` event to the log. The full diagram (Mermaid) is in [planning.md](planning.md#architecture).

## Detection signals

| | Signal 1: LLM judgment (Groq `openai/gpt-oss-20b`) | Signal 2: sentence-length variation |
|---|---|---|
| Measures | Semantic and rhetorical feel: transition density ("Furthermore"), over-balanced tone, generic phrasing, uniform pacing | Coefficient of variation of words per sentence (`s2 = clamp(1 - cv/0.8, 0, 1)`; 1.0 = very uniform) |
| Why it separates | LLMs produce probable, evenly paced, neutral prose; people are idiosyncratic and specific | Human writing is "bursty"; model output tends toward even sentence lengths |
| Blind spots | Formal human prose (academic, corporate) and edited AI text; the LLM judge itself is a fallible model | Texts under about 40 words, poetry or minimalist writing, lists and dialogue (sentence splitting breaks) |
| Output | float 0-1 (`null` if Groq is unavailable) | float 0-1 (0.5 if fewer than 3 sentences) |

They are independent: one reads meaning, the other counts structure.

## Confidence scoring

`P(AI) = 0.65 * s1 + 0.35 * s2`. The LLM gets more weight because it copes with more genres, whereas a purely structural measure is noisy.

| P(AI) | Attribution | Reported confidence |
|---|---|---|
| >= 0.70 | `AI_generated_or_assisted` | P(AI) |
| 0.35 to 0.70 | `No_classification` | max(P, 1-P), so 0.50 to 0.65 |
| <= 0.35 | `Human_made` | 1 - P(AI) |

**What 0.5 means:** the signals carry no usable information. The bands are asymmetric on purpose (0.70 to accuse, 0.35 to clear) because a false positive costs a human creator more than a false negative. Safeguards: texts under 40 words and runs where Groq fails are capped at 0.69, so the system can never issue a confident AI verdict on thin evidence. Signal disagreement also lands in the uncertain band by construction (for example, s1=0.05 with perfectly uniform sentences gives 0.38, which is uncertain).

**How I tested it:** `test_api.py` checks the weights, thresholds, caps and that all three labels are reachable (using stubbed s1 values 0.95 / 0.5 / 0.0). `test_signals.py` runs the four sample inputs from the brief through both signals.

**Calibration run (`python test_signals.py`, both signals live):**

| Input | s1 (LLM) | s2 (style) | P(AI) | Attribution | Confidence |
|---|---|---|---|---|---|
| clearly AI ("Artificial intelligence represents a transformative paradigm shift...") | 0.92 | 0.526 | 0.782 | AI_generated_or_assisted | 0.78 |
| clearly human (casual ramen review) | 0.10 | 0.236 | 0.148 | Human_made | 0.85 |
| formal human (monetary policy) | 0.70 | 0.500 | 0.630 | No_classification | 0.63 |
| lightly edited AI (remote work) | 0.40 | 0.522 | 0.443 | No_classification | 0.56 |

The high-confidence case (0.85 human) and the lower-confidence case (0.63 uncertain) differ clearly, and the four samples cover all three bands. The formal-human sample being scored 0.70 by the LLM is exactly the blind spot named above; it was kept out of the AI band by the weaker stylometric score and would be routed to appeal. The edited-AI sample was *not* flagged (a false negative, accepted by design).

**Caveats:** the LLM score is not perfectly repeatable even at temperature 0 (the same AI sample gave s1=0.92 in one run and 0.85 in another, moving P(AI) from 0.78 to 0.74), and this is only four hand-picked samples, not a benchmark. The 0.70 threshold is therefore a judgment call, not a measured error rate.

## Transparency label (exact text)

| Variant | Shown when | Text |
|---|---|---|
| High-confidence AI | P(AI) >= 0.70 | "Likely AI-generated or AI-assisted. Our automated checks found strong signs that this work was produced with AI. Automated checks can be wrong. If you wrote this yourself, the creator can appeal and a person will review it." |
| High-confidence human | P(AI) <= 0.35 | "Likely human-written. Our automated checks found no strong signs of AI generation. This is an estimate, not a guarantee." |
| Uncertain | 0.35 < P(AI) < 0.70 | "Origin unclear. Our automated checks could not tell whether this was written by a person or with AI. No conclusion is being drawn about this work. The creator can request a review." |
| Under review (after appeal) | status = under_review | "Under review. The creator has contested this label and a person is reviewing it." |

Labels avoid percentages and jargon, hedge the AI verdict, and always point to a human path. I have not yet shown them to an outside reader, as the brief recommends.

## Appeals

Eligible: `No_classification`, or `AI_generated_or_assisted` with P(AI) <= 0.80. `Human_made` or P(AI) > 0.80 returns 400. Other responses: 404 unknown id, 403 wrong creator, 409 already under review, 400 missing reasoning. The appeal stores the reasoning, flips status to `under_review`, and logs an `appeal` event carrying the original attribution, confidence and both signal scores. Re-classification is not automated.

## Rate limiting

`/submit`: **10 per minute; 100 per day** per IP (Flask-Limiter, in-memory storage).
Reasoning: a writer posting their own poem or essay rarely does more than a few per hour, so 10/min covers bursts such as uploading a batch while staying far below a script's speed; 100/day is generous for a human but caps an adversary who tries to probe the detector or burn the Groq quota. In-memory storage resets on restart; production would use Redis. Evidence (12 rapid requests against a fresh server):

```
200 200 200 200 200 200 200 200 200 200 429 429
```

## Audit log

SQLite `audit_log` table, append-only; submissions and appeals are separate events. `GET /log` returns them (newest first). Sample from a real run with both signals live:

```json
{"timestamp": "2026-11-25T16:57:27.542Z", "event": "appeal", "content_id": "e71d7708-7942-4847-93f5-89ccadc27ab5", "creator_id": "demo-formal_human", "attribution": "No_classification", "confidence": 0.63, "llm_score": 0.7, "stylometric_score": 0.5, "signals_used": ["llm", "stylometric"], "status": "under_review", "appeal_filed": true, "appeal_reasoning": "I wrote this myself. I am a non-native English speaker and my writing style may appear more formal than typical."}
{"timestamp": "2026-11-25T16:57:27.535Z", "event": "submission", "content_id": "e71d7708-7942-4847-93f5-89ccadc27ab5", "creator_id": "demo-formal_human", "attribution": "No_classification", "confidence": 0.63, "llm_score": 0.7, "stylometric_score": 0.5, "signals_used": ["llm", "stylometric"], "status": "classified", "appeal_filed": false, "appeal_reasoning": null}
{"timestamp": "2026-11-25T16:57:27.199Z", "event": "submission", "content_id": "240e1f6a-a70f-4a9d-904e-4ea2bd870b78", "creator_id": "demo-clearly_ai", "attribution": "AI_generated_or_assisted", "confidence": 0.7366, "llm_score": 0.85, "stylometric_score": 0.5259, "signals_used": ["llm", "stylometric"], "status": "classified", "appeal_filed": false, "appeal_reasoning": null}
{"timestamp": "2026-11-25T16:57:26.747Z", "event": "submission", "content_id": "5ec5505e-a48a-4410-bb27-52e0c9284aef", "creator_id": "demo-clearly_human", "attribution": "Human_made", "confidence": 0.8524, "llm_score": 0.1, "stylometric_score": 0.236, "signals_used": ["llm", "stylometric"], "status": "classified", "appeal_filed": false, "appeal_reasoning": null}
```

## Known limitations

- **Formal human writing** (an economics abstract, or a non-native speaker's careful prose): even, polished sentences push s2 toward "uniform" and the LLM tends to read formality as AI. The 0.70 bar, the caps and the appeal path exist for this case; the demo's `formal_human` sample came out uncertain, not AI.
- **Minimalist or repetitive poetry**: short equal-length lines make sentence variance meaningless, and very short pieces are capped below 0.70.
- **Lightly edited AI text** will usually be labeled human or uncertain, because editing adds burstiness and removes transition markers (an accepted false negative).
- Signal 2 is a single metric (the planning spec called for sentence-length variance only); type-token ratio or punctuation density would make it sturdier.
- The `/appeal` creator check is optional (`creator_id` is not authenticated), and `/log` and `/appeals` have no auth. This is fine for a class project and not for production.

## Spec reflection

**Helped:** writing the thresholds and weights first meant `combine()` and `classify()` were direct translations, and tests could assert exact boundaries (0.70 and 0.35). **Diverged:** the original spec gave three different threshold ranges in different places (0.35/0.70, 0.4-0.6, 0.5-0.6), so I settled on 0.35/0.70 everywhere. I also added things the spec did not have: the short-text and LLM-failure caps, and defining uncertain-band confidence as max(P, 1-P) after my first version reported "confidence 1.0" for an uncertain result, which read as the opposite of the truth.

## AI usage

- I directed Claude Code to turn my planning.md (signals, weights, thresholds, diagram) into the Flask app, SQLite audit log and endpoints. I reviewed it against the spec; the output matched the weights and thresholds, but its uncertain-band confidence formula produced 1.0 at P=0.5, which I overrode with max(P, 1-P).
- I directed it to write tests. Its first test text was a single 61-word sentence, which gave s2=0 and a mid-band score instead of the AI band; the failure exposed a bad test input rather than a code bug, and I changed it to uniform sentences. Another test expectation (s1=0.05 should be "human") was wrong because the signals disagree by design, so I changed the stub to 0.0.
- The first live-server run was blocked by macOS AirPlay on port 5000; I moved the default to 5001.
- The brief's recommended model (`meta-llama/llama-4-scout-17b-16e-instruct`) returned 404 on this account because Groq has retired it. I listed the account's available models, switched to `openai/gpt-oss-20b` (overridable via `GROQ_MODEL`) and raised `max_tokens` to 1000 because it is a reasoning model. The pinned `groq==0.15.0` SDK rejected a `reasoning_effort` parameter, so I removed it.

## Not done / still to do

- Show the labels to someone who hasn't seen the project and revise if needed.
- Stretch features (ensemble, certificate, dashboard, multi-modal) were not attempted.
