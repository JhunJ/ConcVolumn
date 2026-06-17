"""샘플 3dm → 프로젝트 업로드 + 부재(curve_json) 재추출 + preview 캐시"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLE = ROOT / "Sample" / "안양_지하층모델_수평수직구분.3dm"
PROJECT = "안양 지하층"
FLOOR = "B1"

from app import (
    get_db,
    get_project_strengths,
    sync_floor_members,
    build_and_store_preview_cache,
    init_db,
)
from geometry_engine import extract_floor_members


def curve_stats(members):
    rect = real = empty = 0
    for x in members:
        pc = x.get("plan_curves") or []
        if not pc or not pc[0]:
            empty += 1
            continue
        if len(pc[0]) <= 6:
            rect += 1
        else:
            real += 1
    return {"total": len(members), "real_curves": real, "bbox_like": rect, "empty": empty}


def main():
    init_db()
    if not SAMPLE.is_file():
        print(f"FAIL: sample not found: {SAMPLE}")
        return 1

    print("=== Extract test (RhinoInside) ===")
    defaults = {"기둥": 24.0, "벽체": 24.0, "수평": 24.0}
    extracted = extract_floor_members(str(SAMPLE), defaults)
    stats = curve_stats(extracted)
    print(stats)
    if stats["real_curves"] == 0:
        print("WARN: no real plan curves extracted")

    conn = get_db()
    row = conn.execute(
        "SELECT id FROM projects WHERE project_name=?", (PROJECT,)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO projects (project_name, reg_date) VALUES (?, datetime('now'))",
            (PROJECT,),
        )
        conn.commit()
        pid = conn.execute(
            "SELECT id FROM projects WHERE project_name=?", (PROJECT,)
        ).fetchone()["id"]
        print(f"Created project {PROJECT} id={pid}")
    else:
        pid = row["id"]

    upload_dir = ROOT / "uploads"
    upload_dir.mkdir(exist_ok=True)
    dest = upload_dir / f"{PROJECT}_{FLOOR}_{int(time.time())}.3dm"
    shutil.copy2(SAMPLE, dest)
    print(f"Copied → {dest}")

    fm = conn.execute(
        "SELECT id FROM floor_models WHERE project_id=? AND floor_label=?",
        (pid, FLOOR),
    ).fetchone()

    bbox_raw = None
    if extracted:
        xs = [m["bbox"]["min"]["x"] for m in extracted] + [m["bbox"]["max"]["x"] for m in extracted]
        ys = [m["bbox"]["min"]["y"] for m in extracted] + [m["bbox"]["max"]["y"] for m in extracted]
        zs = [m["bbox"]["min"]["z"] for m in extracted] + [m["bbox"]["max"]["z"] for m in extracted]
        bbox_raw = json.dumps({
            "min": {"x": min(xs), "y": min(ys), "z": min(zs)},
            "max": {"x": max(xs), "y": max(ys), "z": max(zs)},
        })

    if fm:
        fm_id = fm["id"]
        conn.execute(
            """UPDATE floor_models SET file_path=?, bbox_json=?, member_count=0 WHERE id=?""",
            (str(dest), bbox_raw, fm_id),
        )
        print(f"Updated floor_model id={fm_id}")
    else:
        conn.execute(
            """INSERT INTO floor_models (project_id, floor_label, file_path, bbox_json, member_count, reg_date)
               VALUES (?,?,?,?,0, datetime('now'))""",
            (pid, FLOOR, str(dest), bbox_raw),
        )
        fm_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(f"Created floor_model id={fm_id}")

    conn.commit()
    defaults = get_project_strengths(conn, pid) or defaults
    members = sync_floor_members(conn, fm_id, str(dest), defaults)
    conn.commit()

    with_curve = conn.execute(
        """SELECT COUNT(*) AS n FROM floor_members
           WHERE floor_model_id=? AND curve_json IS NOT NULL
           AND curve_json NOT IN ('', '[]')""",
        (fm_id,),
    ).fetchone()["n"]
    print(f"Synced {len(members)} members, DB with curve_json: {with_curve}")

    preview = build_and_store_preview_cache(
        conn, fm_id, bbox_raw, str(dest), defaults, pid, FLOOR,
    )
    conn.commit()
    conn.close()
    print(f"Preview: n={preview.get('n') if preview else 0} members={preview.get('members') if preview else 0}")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
