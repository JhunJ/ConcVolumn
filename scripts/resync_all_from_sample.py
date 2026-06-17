"""curve_json NULL인 모든 층 — 샘플 3dm으로 재업로드·재추출"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLE = ROOT / "Sample" / "안양_지하층모델_수평수직구분.3dm"

import geometry_engine  # noqa: F401
from app import (
    get_db,
    get_project_strengths,
    sync_floor_members,
    build_and_store_preview_cache,
    delete_preview_cache_file,
    init_db,
)
from geometry_engine import extract_floor_members


def resync_floor(conn, fm_id, project_id, floor_label, file_path, bbox_raw, defaults):
    members = sync_floor_members(conn, fm_id, file_path, defaults)
    delete_preview_cache_file(fm_id)
    preview = build_and_store_preview_cache(
        conn, fm_id, bbox_raw, file_path, defaults, project_id, floor_label,
    )
    with_curve = conn.execute(
        """SELECT COUNT(*) AS n FROM floor_members
           WHERE floor_model_id=? AND curve_json IS NOT NULL AND curve_json NOT IN ('','[]')""",
        (fm_id,),
    ).fetchone()["n"]
    real = conn.execute(
        """SELECT COUNT(*) AS n FROM floor_members
           WHERE floor_model_id=? AND curve_json IS NOT NULL
           AND length(curve_json) > 80""",
        (fm_id,),
    ).fetchone()["n"]
    return len(members), with_curve, real, preview.get("n") if preview else 0


def main():
    init_db()
    if not SAMPLE.is_file():
        print("FAIL: sample missing", SAMPLE)
        return 1

    defaults = {"기둥": 24.0, "벽체": 24.0, "수평": 24.0}
    extracted = extract_floor_members(str(SAMPLE), defaults)
    xs = [m["bbox"]["min"]["x"] for m in extracted] + [m["bbox"]["max"]["x"] for m in extracted]
    ys = [m["bbox"]["min"]["y"] for m in extracted] + [m["bbox"]["max"]["y"] for m in extracted]
    zs = [m["bbox"]["min"]["z"] for m in extracted] + [m["bbox"]["max"]["z"] for m in extracted]
    bbox_raw = json.dumps({
        "min": {"x": min(xs), "y": min(ys), "z": min(zs)},
        "max": {"x": max(xs), "y": max(ys), "z": max(zs)},
    })

    upload_dir = ROOT / "uploads"
    upload_dir.mkdir(exist_ok=True)

    conn = get_db()
    rows = conn.execute(
        """SELECT fm.id, fm.floor_label, p.id AS pid, p.project_name
           FROM floor_models fm JOIN projects p ON p.id = fm.project_id"""
    ).fetchall()

    for r in rows:
        pn, fl, fm_id, pid = r["project_name"], r["floor_label"], r["id"], r["pid"]
        dest = upload_dir / f"{pn}_{fl}_{int(time.time())}.3dm"
        shutil.copy2(SAMPLE, dest)
        defs = get_project_strengths(conn, pid) or defaults
        conn.execute(
            "UPDATE floor_models SET file_path=?, bbox_json=?, member_count=0 WHERE id=?",
            (str(dest), bbox_raw, fm_id),
        )
        conn.commit()
        n, wc, real, segs = resync_floor(conn, fm_id, pid, fl, str(dest), bbox_raw, defs)
        conn.commit()
        print(f"{pn}/{fl} fm={fm_id}: members={n} curve_json={wc} complex={real} preview_segs={segs}")

    conn.close()
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
