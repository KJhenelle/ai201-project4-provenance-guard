"""Generate audit-log evidence against a running server: python demo.py [base_url]"""
import json, sys, urllib.request, urllib.error
from test_signals import SAMPLES
B = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5001"

def post(path, body):
    req = urllib.request.Request(B + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"http": e.code}

ids = {}
for k in ("clearly_human", "clearly_ai", "formal_human"):
    r = post("/submit", {"text": SAMPLES[k], "creator_id": f"demo-{k}"})
    ids[k] = r["content_id"]; print(k, r["attribution"], r["confidence"], r["signals"])
print(post("/appeal", {"content_id": ids["formal_human"], "creator_id": "demo-formal_human",
      "creator_reasoning": "I wrote this myself. I am a non-native English speaker and my writing style may appear more formal than typical."}))
print(json.dumps(json.load(urllib.request.urlopen(B + "/log")), indent=2))
