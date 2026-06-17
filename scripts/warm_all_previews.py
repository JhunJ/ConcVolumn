"""모든 층 2D preview gzip + WebP raster 일괄 생성"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import (
    get_db,
    get_project_strengths,
    build_and_store_preview_cache,
    _preview_cache_path,
    _preview_raster_path,
)

def main():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT fm.id, fm.file_path, fm.bbox_json, fm.floor_label,
               p.id AS project_id, p.project_name
        FROM floor_models fm
        JOIN projects p ON p.id = fm.project_id
        ORDER BY p.project_name, fm.floor_label
        """
    ).fetchall()

    ok, fail = 0, 0
    for r in rows:
        fm_id = r["id"]
        pn, fl = r["project_name"], r["floor_label"]
        print(f"--- {pn} / {fl} (fm={fm_id})")
        defaults = get_project_strengths(conn, r["project_id"])
        preview = build_and_store_preview_cache(
            conn,
            fm_id,
            r["bbox_json"],
            r["file_path"],
            defaults,
            r["project_id"],
            fl,
        )
        if not preview:
            print("  FAIL: preview empty")
            fail += 1
            continue
        gz = _preview_cache_path(fm_id)
        webp = _preview_raster_path(fm_id)
        print(f"  n={preview.get('n', 0)} members={preview.get('members', 0)}")
        print(f"  gzip: {gz.is_file()} ({gz.stat().st_size if gz.is_file() else 0:,} B)")
        print(f"  webp: {webp.is_file()} ({webp.stat().st_size if webp.is_file() else 0:,} B)")
        ok += 1

    conn.commit()
    conn.close()
    print(f"\nDONE ok={ok} fail={fail}")

if __name__ == "__main__":
    main()
