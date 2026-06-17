import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import get_db, load_preview_cache_file


def bbox_rect(bbox):
    mn, mx = bbox["min"], bbox["max"]
    return [
        [mn["x"], mn["y"]], [mx["x"], mn["y"]],
        [mx["x"], mx["y"]], [mn["x"], mx["y"]], [mn["x"], mn["y"]],
    ]


def same_poly(a, b, tol=1.0):
    if not a or not b or len(a) != len(b):
        return False
    return all(abs(a[i][0] - b[i][0]) < tol and abs(a[i][1] - b[i][1]) < tol for i in range(len(a)))


conn = get_db()
fm = conn.execute(
    "SELECT fm.id FROM floor_models fm JOIN projects p ON p.id=fm.project_id WHERE p.project_name=?",
    ("개편테스트",),
).fetchone()
fid = fm["id"]

rows = conn.execute(
    "SELECT part_type, curve_json, bbox_json FROM floor_members WHERE floor_model_id=?",
    (fid,),
).fetchall()

by_part = {}
for r in rows:
    part = r["part_type"]
    by_part.setdefault(part, {"total": 0, "null": 0, "eq_bbox": 0, "le6": 0, "gt6": 0})
    by_part[part]["total"] += 1
    if not r["curve_json"]:
        by_part[part]["null"] += 1
        continue
    curves = json.loads(r["curve_json"])
    if not curves or not curves[0]:
        by_part[part]["null"] += 1
        continue
    poly = curves[0]
    n = len(poly)
    if n <= 6:
        by_part[part]["le6"] += 1
    else:
        by_part[part]["gt6"] += 1
    bbox = json.loads(r["bbox_json"])
    if same_poly(poly, bbox_rect(bbox)):
        by_part[part]["eq_bbox"] += 1

print("=== curve_json vs bbox ===")
for part, s in by_part.items():
    print(part, s)

prev = load_preview_cache_file(fid)
segs = prev.get("segments", [])
print("\n=== preview segments ===")
from collections import Counter
c = Counter((s["part"], len(s["p"]), s.get("so"), s.get("f") is not None) for s in segs)
for k, v in c.most_common(12):
    print(k, v)
