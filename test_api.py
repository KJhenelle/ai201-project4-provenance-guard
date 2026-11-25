"""End-to-end tests with a stubbed LLM signal (no network). Run: python test_api.py"""
import os, tempfile
os.environ["PG_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import app as A

LONG = "This is a plain sentence with eight words total. " * 8
def run(s1):
    A.llm_signal = lambda t: s1
    return A.app.test_client()

def test_all_labels_and_appeals():
    c = A.app.test_client()
    out = {}
    for name, s1 in [("ai", 0.95), ("unc", 0.5), ("human", 0.0)]:
        A.llm_signal = lambda t, s1=s1: s1
        r = c.post("/submit", json={"text": LONG, "creator_id": "u1"}, environ_overrides={"REMOTE_ADDR": f"10.0.0.{len(out)}"}).get_json()
        out[name] = r
    assert out["ai"]["attribution"] == "AI_generated_or_assisted" and out["ai"]["label"] == A.LABELS["AI_generated_or_assisted"]
    assert out["unc"]["attribution"] == "No_classification"
    assert out["human"]["attribution"] == "Human_made"
    assert len({o["label"] for o in out.values()}) == 3
    # appeal flow
    cid = out["unc"]["content_id"]
    assert c.post("/appeal", json={"content_id": cid}).status_code == 400            # no reasoning
    assert c.post("/appeal", json={"content_id": cid, "creator_reasoning": "mine", "creator_id": "evil"}).status_code == 403
    r = c.post("/appeal", json={"content_id": cid, "creator_reasoning": "I wrote it", "creator_id": "u1"})
    assert r.status_code == 200 and r.get_json()["status"] == "under_review"
    assert c.post("/appeal", json={"content_id": cid, "creator_reasoning": "again"}).status_code == 409
    assert c.post("/appeal", json={"content_id": out["human"]["content_id"], "creator_reasoning": "x"}).status_code == 400
    assert c.post("/appeal", json={"content_id": "nope", "creator_reasoning": "x"}).status_code == 404
    assert any(e["event"] == "appeal" and e["status"] == "under_review" and e["appeal_reasoning"] for e in c.get("/log").get_json()["entries"])
    assert len(c.get("/appeals").get_json()["queue"]) == 1

def test_caps():
    assert A.combine(1.0, 1.0, 10)[0] <= 0.69          # short text
    assert A.combine(None, 1.0, 200)[0] <= 0.69        # LLM down
    assert A.combine(0.9, 0.9, 200)[0] == 0.9
    assert A.classify(0.70)[0] == "AI_generated_or_assisted" and A.classify(0.35)[0] == "Human_made"

if __name__ == "__main__":
    test_caps(); test_all_labels_and_appeals(); print("all tests passed")
