"""개편테스트 2D curve 품질 진단"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import geometry_engine  # noqa: F401
from app import get_db, load_preview_cache_file


def bbox_rect(bbox):
    mn, mx = bbox["min"], bbox["max"]
    return [
        [mn["x"], mn["y"]], [mx["x"], mn["y"]],
        [mx["x"], mx["y"]], [mn["x"], mx["y"]], [mn["x"], mn["y"]],
    ]


def is_axis_aligned_rect(poly, tol=1.0):
    if not poly or len(poly) < 4:
        return False
    pts = poly[:-1] if len(poly) > 1 and poly[0] == poly[-1] else poly
    if len(pts) != 4:
        return False
    xs = sorted(set(round(p[0] / tol) * tol for p in pts))
    ys = sorted(set(round(p[1] / tol) * tol for p in pts))
    return len(xs) == 2 and len(ys) == 2


def same_as_bbox(poly, bbox, tol=1.0):
    return is_axis_aligned_rect(poly, tol) and is_axis_aligned_rect(bbox_rect(bbox), tol) and abs(
        _area(poly) - _area(bbox_rect(bbox))
    ) < max(_area(bbox_rect(bbox)) * 0.01, 100)


def _area(poly):
    pts = poly[:-1] if poly[0] == poly[-1] else poly
    a = 0.0
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        a += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]
    return abs(a) / 2


conn = get_db()
fm = conn.execute(
    "SELECT fm.id, fm.file_path FROM floor_models fm "
    "JOIN projects p ON p.id=fm.project_id WHERE p.project_name=?",
    ("개편테스트",),
).fetchone()
fid = fm["id"]
print("fm_id", fid, "file", fm["file_path"])

rows = conn.execute(
    "SELECT part_type, curve_json, bbox_json FROM floor_members WHERE floor_model_id=?",
    (fid,),
).fetchall()

by_part = {}
for r in rows:
    part = r["part_type"]
    by_part.setdefault(part, {"null": 0, "bbox_match": 0, "aabb": 0, "complex": 0, "total": 0})
    by_part[part]["total"] += 1
    if not r["curve_json"]:
        by_part[part]["null"] += 1
        continue
    curves = json.loads(r["curve_json"])
    if not curves or not curves[0]:
        by_part[part]["null"] += 1
        continue
    poly = curves[0]
    bbox = json.loads(r["bbox_json"])
    if same_as_bbox(poly, bbox):
        by_part[part]["bbox_match"] += 1
    elif is_axis_aligned_rect(poly):
        by_part[part]["aabb"] += 1
    else:
        by_part[part]["complex"] += 1

print("\n=== DB curve_json ===")
for part, s in by_part.items():
    print(part, s)

prev = load_preview_cache_file(fid)
segs = prev.get("segments", [])
print("\n=== preview segments ===")
print("total", len(segs), "circles", sum(1 for s in segs if s.get("kind") == "circle"))
pstats = {"bbox_match": 0, "aabb": 0, "complex": 0, "no_p": 0}
for s in segs:
    if not s.get("p"):
        pstats["no_p"] += 1
        continue
    poly = s["p"]
    if len(poly) <= 6 and is_axis_aligned_rect(poly):
        pstats["aabb"] += 1
    elif len(poly) > 6:
        pstats["complex"] += 1
    else:
        pstats["complex"] += 1
print(pstats)
print("sample wall complex", next((s for s in segs if s.get("part") == "벽체" and len(s.get("p", [])) > 10), None))
print("sample col", next((s for s in segs if s.get("part") == "기둥"), None))
