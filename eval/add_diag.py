import json
from pathlib import Path
p = Path(__file__).parent / "judge_cases.json"
d = json.loads(p.read_text(encoding="utf-8"))
new = [
  {"id": "d_hard_ok",  "class": "diag", "scored": False, "expected": "-", "text": "13 × 17 = 221."},
  {"id": "d_hard_ok2", "class": "diag", "scored": False, "expected": "-", "text": "29 × 31 = 899."},
  {"id": "d_easy_bad", "class": "diag", "scored": False, "expected": "-", "text": "12 × 12 = 150."},
  {"id": "d_hard_bad", "class": "diag", "scored": False, "expected": "-", "text": "17 × 23 = 400."}
]
ids = {c["id"] for c in d["cases"]}
d["cases"] += [c for c in new if c["id"] not in ids]
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
print("cases:", len(d["cases"]))