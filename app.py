"""
ConCast - 콘크리트 타설 물량 관리 시스템
Concrete Casting Volume Management
"""
import gzip
import os, json, sqlite3, time, threading, uuid, re, colorsys, math
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from geometry_engine import (
    extract_model_metadata,
    extract_preview_geometry,
    extract_3d_mesh,
    extract_floor_members,
    extract_all_plan_curves,
    extract_members_preview,
    calculate_polygon_intersection_volume,
    calculate_zone_volume_breakdown,
    compute_remaining_polygon,
    compute_snap_size,
    DEFAULT_STRENGTHS,
    RHINOINSIDE_AVAILABLE,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
PREVIEW_CACHE_DIR = BASE_DIR / "preview_cache"
PREVIEW_CACHE_DIR.mkdir(exist_ok=True)
PREVIEW_FMT_VERSION = 3
DB_PATH = BASE_DIR / "concast.db"

TRUCK_VOL_M3 = 6.0
TRUCK_MIN_VOL_M3 = 3.0


def trucks_from_volume(volume: float) -> float:
    """레미콘 대수: 6m³/대, 0.5대 단위 올림, 3m³ 이하는 최소 0.5대."""
    if volume <= 0:
        return 0.0
    trucks = volume / TRUCK_VOL_M3
    trucks = math.ceil(trucks * 2 - 1e-9) / 2.0
    if volume <= TRUCK_MIN_VOL_M3 and trucks < 0.5:
        return 0.5
    return trucks


app = FastAPI(title="ConCast - 콘크리트 타설 물량 관리", version="4.0.0")
app.add_middleware(GZipMiddleware, minimum_size=500)


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT UNIQUE NOT NULL,
            reg_date TEXT NOT NULL,
            default_strengths_json TEXT
        );
        CREATE TABLE IF NOT EXISTS floor_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            floor_label TEXT NOT NULL,
            file_path TEXT NOT NULL,
            bbox_json TEXT,
            object_count INTEGER DEFAULT 0,
            member_count INTEGER DEFAULT 0,
            reg_date TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id),
            UNIQUE(project_id, floor_label)
        );
        CREATE TABLE IF NOT EXISTS floor_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            floor_model_id INTEGER NOT NULL,
            object_index INTEGER NOT NULL,
            part_type TEXT NOT NULL,
            orientation TEXT NOT NULL,
            rhino_value TEXT,
            strength_override REAL,
            full_volume REAL DEFAULT 0,
            centroid_json TEXT,
            bbox_json TEXT,
            FOREIGN KEY (floor_model_id) REFERENCES floor_models(id) ON DELETE CASCADE,
            UNIQUE(floor_model_id, object_index)
        );
        CREATE TABLE IF NOT EXISTS strength_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            floor_label TEXT NOT NULL,
            zone_name TEXT NOT NULL,
            polygon_json TEXT NOT NULL,
            strength REAL NOT NULL DEFAULT 35,
            target_parts_json TEXT DEFAULT '["기둥","벽체"]',
            color TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
        CREATE TABLE IF NOT EXISTS pour_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            floor_label TEXT NOT NULL,
            zone_name TEXT NOT NULL,
            polygon_json TEXT NOT NULL,
            status TEXT DEFAULT 'planned',
            calculated_volume REAL DEFAULT 0,
            volume_horizontal REAL DEFAULT 0,
            volume_vertical REAL DEFAULT 0,
            volume_by_strength_json TEXT,
            calc_breakdown_json TEXT,
            actual_volume REAL,
            planned_date TEXT,
            completed_date TEXT,
            color TEXT,
            memo TEXT,
            intersection_mesh_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS actual_pours (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id INTEGER NOT NULL,
            pour_label TEXT NOT NULL,
            polygon_json TEXT NOT NULL,
            pour_date TEXT,
            volume REAL DEFAULT 0,
            volume_by_strength_json TEXT,
            intersection_mesh_json TEXT,
            memo TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (zone_id) REFERENCES pour_zones(id) ON DELETE CASCADE
        );
    """)
    # 기존 DB 마이그레이션
    migrations = [
        "ALTER TABLE pour_zones ADD COLUMN intersection_mesh_json TEXT",
        "ALTER TABLE pour_zones ADD COLUMN volume_horizontal REAL DEFAULT 0",
        "ALTER TABLE pour_zones ADD COLUMN volume_vertical REAL DEFAULT 0",
        "ALTER TABLE pour_zones ADD COLUMN volume_by_strength_json TEXT",
        "ALTER TABLE pour_zones ADD COLUMN calc_breakdown_json TEXT",
        "ALTER TABLE projects ADD COLUMN default_strengths_json TEXT",
        "ALTER TABLE floor_models ADD COLUMN member_count INTEGER DEFAULT 0",
        "ALTER TABLE floor_members ADD COLUMN curve_json TEXT",
        "ALTER TABLE floor_models ADD COLUMN preview_cache_json TEXT",
        "ALTER TABLE floor_models ADD COLUMN preview_cache_at TEXT",
        "ALTER TABLE floor_models ADD COLUMN preview_cache_file TEXT",
        "ALTER TABLE actual_pours ADD COLUMN volume_by_strength_json TEXT",
        "ALTER TABLE actual_pours ADD COLUMN intersection_mesh_json TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass
    # 컬럼 누락 시 재시도 (구버전 actual_pours 테이블)
    ap_cols = {r[1] for r in conn.execute("PRAGMA table_info(actual_pours)").fetchall()}
    if "intersection_mesh_json" not in ap_cols:
        try:
            conn.execute("ALTER TABLE actual_pours ADD COLUMN intersection_mesh_json TEXT")
        except Exception:
            pass
    if "volume_by_strength_json" not in ap_cols:
        try:
            conn.execute("ALTER TABLE actual_pours ADD COLUMN volume_by_strength_json TEXT")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _default_strengths_json() -> str:
    return json.dumps({}, ensure_ascii=False)


def get_project_strengths(conn, project_id: int) -> dict:
    row = conn.execute("SELECT default_strengths_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if row and row["default_strengths_json"]:
        try:
            return json.loads(row["default_strengths_json"])
        except Exception:
            pass
    return {}


def sync_floor_members(conn, floor_model_id: int, file_path: str, default_strengths: dict):
    conn.execute("DELETE FROM floor_members WHERE floor_model_id=?", (floor_model_id,))
    members = extract_floor_members(file_path, default_strengths)
    for m in members:
        conn.execute(
            """INSERT INTO floor_members
               (floor_model_id, object_index, part_type, orientation, rhino_value,
                full_volume, centroid_json, bbox_json, curve_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (floor_model_id, m["object_index"], m["part_type"], m["orientation"],
             m.get("rhino_value"), m["full_volume"],
             json.dumps(m["centroid"]), json.dumps(m["bbox"]),
             json.dumps(m.get("plan_curves", []), ensure_ascii=False)),
        )
    conn.execute("UPDATE floor_models SET member_count=? WHERE id=?", (len(members), floor_model_id))
    return members


def get_strength_zones_for_floor(conn, project_id: int, floor_label: str) -> list:
    rows = conn.execute(
        "SELECT * FROM strength_zones WHERE project_id=? AND floor_label=? ORDER BY created_at",
        (project_id, floor_label),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["polygon"] = json.loads(d["polygon_json"])
        d["target_parts"] = json.loads(d["target_parts_json"]) if d.get("target_parts_json") else ["기둥", "벽체"]
        del d["polygon_json"]
        del d["target_parts_json"]
        result.append(d)
    return result


def build_member_curves_from_db(conn, floor_model_id: int) -> list:
    """2D plan view — DB에 저장된 부재별 외곽 curve"""
    from geometry_engine import PART_PLAN_STYLE
    rows = conn.execute(
        """SELECT object_index, part_type, orientation, curve_json, bbox_json
           FROM floor_members WHERE floor_model_id=?""",
        (floor_model_id,),
    ).fetchall()
    curves = []
    for r in rows:
        part = r["part_type"] or "미분류"
        style = PART_PLAN_STYLE.get(part, PART_PLAN_STYLE.get("벽체"))
        plan_curves = []
        if r["curve_json"]:
            try:
                plan_curves = json.loads(r["curve_json"])
            except Exception:
                pass
        if not plan_curves:
            continue
        curves.append({
            "object_index": r["object_index"],
            "part_type": part,
            "orientation": r["orientation"],
            "face": "top" if part == "수평" else "bottom",
            "curves": plan_curves,
            "fill": style["fill"],
            "stroke": style["stroke"],
            "line_width": style["width"],
        })
    return curves


def _parse_bbox(bbox_raw) -> Optional[dict]:
    if bbox_raw is None:
        return None
    if isinstance(bbox_raw, dict):
        return bbox_raw
    try:
        return json.loads(bbox_raw)
    except Exception:
        return None


def assemble_preview_payload(bbox, member_curves: list) -> dict:
    return {
        "bbox": bbox,
        "member_curves": member_curves,
        "snap_size": compute_snap_size(bbox),
    }


_DISPLAY_PTS = {"수평": 256, "벽체": 128, "기둥": 48, "미분류": 64}


def _compact_poly_2d(raw: list, part: str) -> list:
    max_pts = _DISPLAY_PTS.get(part, 32)
    pts = [[round(float(p[0])), round(float(p[1]))] for p in raw if len(p) >= 2]
    if len(pts) < 3:
        return []
    if len(pts) > max_pts:
        step = max(1, len(pts) // max_pts)
        pts = pts[::step]
    if pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]:
        pts.append(pts[0])
    return pts


def _hsl_to_hsla(hsl_color: str, alpha: float) -> str:
    if hsl_color.startswith("hsl(") and not hsl_color.startswith("hsla("):
        inner = hsl_color[4:-1].strip()
        return f"hsla({inner}, {alpha})"
    return hsl_color


def plan_style_for_member(part_type: str, strength: Optional[float], orientation: str) -> dict:
    from geometry_engine import PART_PLAN_STYLE, mesh_display_color, normalize_part_type
    part = normalize_part_type(part_type)
    base = PART_PLAN_STYLE.get(part, PART_PLAN_STYLE["벽체"])
    accent = mesh_display_color(part, strength, orientation)
    stroke_only = bool(base.get("stroke_only")) or part == "수평"
    fill_alpha = {"수평": 0.10, "벽체": 0.14, "기둥": 0.18, "미분류": 0.12}.get(part, 0.12)
    if stroke_only:
        return {"s": accent, "w": base["width"], "so": True, "f": None}
    fill = _hsl_to_hsla(accent, fill_alpha) if accent.startswith("hsl") else base["fill"]
    return {"s": accent, "f": fill, "w": base["width"], "so": False}


def _segment_from_poly(part: str, style: dict, poly: list) -> dict:
    """2D segment — Rhino plan curve 폴리라인 그대로 사용"""
    seg_style = dict(style)
    if len(poly) <= 6 or part in ("기둥",):
        seg_style["so"] = True
        seg_style["f"] = None
    return {
        "part": part,
        "s": seg_style["s"],
        "f": seg_style.get("f"),
        "w": seg_style["w"],
        "so": seg_style.get("so", False),
        "p": poly,
    }


def build_compact_2d_preview(member_items: list, bbox) -> dict:
    """2D 뷰어용 — 부재별·강도별 색상 segments"""
    from geometry_engine import normalize_part_type
    segments = []
    for item in member_items:
        part = normalize_part_type(item.get("part_type") or "미분류")
        style = plan_style_for_member(part, item.get("strength"), item.get("orientation") or "vertical")
        for raw in item.get("curves") or []:
            poly = _compact_poly_2d(raw, part)
            if not poly:
                continue
            segments.append(_segment_from_poly(part, style, poly))
    return {
        "v": PREVIEW_FMT_VERSION,
        "bbox": bbox,
        "snap_size": compute_snap_size(bbox),
        "segments": segments,
        "n": len(segments),
        "members": len(member_items),
    }


def build_compact_2d_preview_legacy(member_curves: list, bbox) -> dict:
    """2D 뷰어용 경량 payload — 업로드/재추출 시 1회 생성"""
    from geometry_engine import PART_PLAN_STYLE
    layers = {}
    for part, style in PART_PLAN_STYLE.items():
        layers[part] = {
            "s": style["stroke"],
            "f": style["fill"],
            "w": style["width"],
            "so": bool(style.get("stroke_only")),
            "p": [],
        }
    poly_count = 0
    for item in member_curves:
        part = item.get("part_type") or "미분류"
        key = part if part in layers else "벽체"
        for raw in item.get("curves") or []:
            poly = _compact_poly_2d(raw, part)
            if poly:
                layers[key]["p"].append(poly)
                poly_count += 1
    return {
        "v": PREVIEW_FMT_VERSION,
        "bbox": bbox,
        "snap_size": compute_snap_size(bbox),
        "layers": layers,
        "n": poly_count,
        "members": len(member_curves),
    }


def _preview_raster_path(floor_model_id: int) -> Path:
    return PREVIEW_CACHE_DIR / f"fm_{floor_model_id}.plan.webp"


def _parse_css_rgba(color: str) -> tuple:
    """CSS rgba/hsla → (r,g,b,a) 0-255"""
    if not color:
        return (148, 163, 184, 180)
    c = color.strip()
    m = re.match(r"rgba?\(\s*([^)]+)\)", c, re.I)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        r, g, b = int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
        a = 255
        if len(parts) > 3:
            a = int(float(parts[3]) * 255) if float(parts[3]) <= 1 else int(float(parts[3]))
        return (r, g, b, a)
    m = re.match(r"hsla?\(\s*([^)]+)\)", c, re.I)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        h = float(parts[0])
        s = float(parts[1].rstrip("%")) / 100.0
        l = float(parts[2].rstrip("%")) / 100.0
        a = 255
        if len(parts) > 3:
            av = float(parts[3].rstrip("%"))
            a = int(av * 255) if av <= 1 else int(av)
        r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
        return (int(r * 255), int(g * 255), int(b * 255), a)
    if c.startswith("#") and len(c) >= 7:
        return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), 255)
    return (148, 163, 184, 180)


def _payload_to_segments(payload: dict) -> list:
    if payload.get("segments"):
        return payload["segments"]
    layers = payload.get("layers") or {}
    segs = []
    for part, layer in layers.items():
        for poly in layer.get("p") or []:
            segs.append({
                "part": part,
                "s": layer.get("s"),
                "f": layer.get("f"),
                "w": layer.get("w", 0.6),
                "so": layer.get("so"),
                "p": poly,
            })
    return segs


def render_plan_raster(payload: dict, out_path: Path, max_px: int = 4096) -> Optional[dict]:
    """PileXY식 — 서버에서 2D 평면 WebP 1회 렌더 (클라이언트 벡터 bake 생략)"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    bbox = payload.get("bbox")
    segments = _payload_to_segments(payload)
    if not bbox or not segments:
        return None

    minx, maxx = float(bbox["min"]["x"]), float(bbox["max"]["x"])
    miny, maxy = float(bbox["min"]["y"]), float(bbox["max"]["y"])
    dx, dy = max(maxx - minx, 1.0), max(maxy - miny, 1.0)
    margin = 0.04
    wx0 = minx - dx * margin
    wx1 = maxx + dx * margin
    wy0 = miny - dy * margin
    wy1 = maxy + dy * margin
    wdx, wdy = wx1 - wx0, wy1 - wy0

    if wdx >= wdy:
        iw = max_px
        ih = max(320, int(max_px * wdy / wdx))
    else:
        ih = max_px
        iw = max(320, int(max_px * wdx / wdy))

    img = Image.new("RGBA", (iw, ih), (10, 14, 26, 255))
    draw = ImageDraw.Draw(img, "RGBA")

    def w2p(x: float, y: float) -> tuple:
        px = (x - wx0) / wdx * iw
        py = (wy1 - y) / wdy * ih
        return (px, py)

    part_order = {"수평": 0, "벽체": 1, "기둥": 2, "미분류": 1}
    ordered = sorted(segments, key=lambda s: part_order.get(s.get("part"), 1))

    for seg in ordered:
        poly = seg.get("p")
        if not poly or len(poly) < 3:
            continue
        pts = [w2p(float(p[0]), float(p[1])) for p in poly if len(p) >= 2]
        if len(pts) < 3:
            continue
        stroke = _parse_css_rgba(seg.get("s") or "rgba(148,163,184,180)")
        fill = None
        if not seg.get("so") and seg.get("f") and seg.get("part") != "수평":
            fill = _parse_css_rgba(seg["f"])
        if fill and fill[3] > 0:
            draw.polygon(pts, fill=fill)
        draw.line(pts + [pts[0]], fill=stroke, width=1)

    tmp = out_path.with_suffix(".tmp.webp")
    img.convert("RGB").save(tmp, "WEBP", quality=85, method=4)
    tmp.replace(out_path)
    return {
        "world": {"min": {"x": wx0, "y": wy0}, "max": {"x": wx1, "y": wy1}},
        "w": iw,
        "h": ih,
    }


def ensure_preview_raster(floor_model_id: int, payload: Optional[dict] = None) -> bool:
    path = _preview_raster_path(floor_model_id)
    if path.is_file():
        return True
    if payload is None:
        payload = load_preview_cache_file(floor_model_id)
    if not payload:
        return False
    meta = render_plan_raster(payload, path)
    if meta and payload is not None:
        payload["raster"] = meta
        save_preview_cache_file(floor_model_id, payload)
    return path.is_file()


def _preview_cache_path(floor_model_id: int) -> Path:
    return PREVIEW_CACHE_DIR / f"fm_{floor_model_id}.v3.json.gz"


def save_preview_cache_file(floor_model_id: int, payload: dict) -> str:
    path = _preview_cache_path(floor_model_id)
    tmp = path.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    return str(path)


def load_preview_cache_file(floor_model_id: int) -> Optional[dict]:
    path = _preview_cache_path(floor_model_id)
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("v") == PREVIEW_FMT_VERSION and data.get("segments"):
            return data
        if data.get("v") == 2 and data.get("layers"):
            return data
    except Exception:
        pass
    return None


def delete_preview_cache_file(floor_model_id: int) -> None:
    path = _preview_cache_path(floor_model_id)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        _preview_raster_path(floor_model_id).unlink(missing_ok=True)
    except Exception:
        pass


def build_preview_from_members(
    conn, floor_model_id: int, bbox_raw, file_path: str, default_strengths: dict,
    project_id: Optional[int] = None, floor_label: Optional[str] = None,
) -> dict:
    from geometry_engine import resolve_member_strength
    bbox = _parse_bbox(bbox_raw)
    strength_zones = get_strength_zones_for_floor(conn, project_id, floor_label) if project_id and floor_label else []
    overrides = get_member_overrides(conn, floor_model_id)
    rows = conn.execute(
        """SELECT object_index, part_type, orientation, rhino_value, strength_override,
                  centroid_json, curve_json, bbox_json
           FROM floor_members WHERE floor_model_id=? ORDER BY object_index""",
        (floor_model_id,),
    ).fetchall()
    member_items = []
    for r in rows:
        centroid = json.loads(r["centroid_json"]) if r["centroid_json"] else {"x": 0, "y": 0}
        strength = resolve_member_strength(
            r["part_type"], r["rhino_value"], centroid,
            strength_zones, r["strength_override"], default_strengths,
        )
        plan_curves = []
        if r["curve_json"]:
            try:
                plan_curves = json.loads(r["curve_json"])
            except Exception:
                pass
        if not plan_curves:
            continue
        member_items.append({
            "part_type": r["part_type"],
            "orientation": r["orientation"],
            "strength": strength,
            "curves": plan_curves,
        })
    if not member_items and file_path:
        legacy = assemble_preview_from_file(file_path, default_strengths, bbox)
        for item in legacy.get("member_curves") or []:
            member_items.append({
                "part_type": item.get("part_type"),
                "orientation": item.get("orientation", "vertical"),
                "strength": None,
                "curves": item.get("curves") or [],
            })
    return build_compact_2d_preview(member_items, bbox)


def build_and_store_preview_cache(
    conn, floor_model_id: int, bbox_raw, file_path: str = "", default_strengths: Optional[dict] = None,
    project_id: Optional[int] = None, floor_label: Optional[str] = None,
) -> Optional[dict]:
    """2D preview를 gzip 파일로 전처리 저장"""
    defaults = default_strengths if default_strengths is not None else {}
    payload = build_preview_from_members(
        conn, floor_model_id, bbox_raw, file_path, defaults, project_id, floor_label,
    )
    if not payload.get("n"):
        return None
    cache_path = save_preview_cache_file(floor_model_id, payload)
    now = datetime.now().isoformat()
    conn.execute(
        """UPDATE floor_models SET preview_cache_json=NULL, preview_cache_at=?,
           preview_cache_file=? WHERE id=?""",
        (now, cache_path, floor_model_id),
    )
    return payload


def assemble_preview_from_file(file_path: str, default_strengths: dict, bbox) -> dict:
    from geometry_engine import PART_PLAN_STYLE
    raw = extract_all_plan_curves(file_path, default_strengths)
    member_curves = []
    for item in raw:
        part = item.get("part_type") or "미분류"
        style = PART_PLAN_STYLE.get(part, PART_PLAN_STYLE.get("벽체"))
        member_curves.append({
            **item,
            "fill": style["fill"],
            "stroke": style["stroke"],
            "line_width": style["width"],
        })
    return assemble_preview_payload(bbox, member_curves)


def build_members_preview_from_db(conn, floor_model_id: int, default_strengths: dict,
                                  strength_zones: list, overrides: dict) -> list:
    """레거시 footprint — member_curves 사용 권장"""
    return build_member_curves_from_db(conn, floor_model_id)


def get_member_overrides(conn, floor_model_id: int) -> dict:
    rows = conn.execute(
        "SELECT object_index, strength_override FROM floor_members WHERE floor_model_id=? AND strength_override IS NOT NULL",
        (floor_model_id,),
    ).fetchall()
    return {r["object_index"]: r["strength_override"] for r in rows}

init_db()


# --- Pydantic ---
class ZoneCreate(BaseModel):
    project_name: str
    floor_label: str
    zone_name: str
    polygon: List[dict]
    status: str = "planned"
    planned_date: Optional[str] = None
    memo: Optional[str] = None

class ZoneUpdate(BaseModel):
    zone_name: Optional[str] = None
    polygon: Optional[List[dict]] = None
    status: Optional[str] = None
    actual_volume: Optional[float] = None
    planned_date: Optional[str] = None
    completed_date: Optional[str] = None
    memo: Optional[str] = None

class CalcRequest(BaseModel):
    project_name: str
    zone_id: int

class ActualPourCreate(BaseModel):
    zone_id: int
    pour_label: str
    polygon: List[dict]
    pour_date: Optional[str] = None
    memo: Optional[str] = None

class ActualPourUpdate(BaseModel):
    pour_label: Optional[str] = None
    polygon: Optional[List[dict]] = None
    pour_date: Optional[str] = None
    volume: Optional[float] = None
    memo: Optional[str] = None

class StrengthZoneCreate(BaseModel):
    project_name: str
    floor_label: str
    zone_name: str
    polygon: List[dict]
    strength: float
    target_parts: List[str]

class StrengthZoneUpdate(BaseModel):
    zone_name: Optional[str] = None
    polygon: Optional[List[dict]] = None
    strength: Optional[float] = None
    target_parts: Optional[List[str]] = None

class MemberStrengthUpdate(BaseModel):
    strength_override: Optional[float] = None

class ProjectStrengthsUpdate(BaseModel):
    default_strengths: dict


# --- Routes ---
@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "index.html"))


@app.get("/engine-info")
def engine_info():
    return {
        "rhinoinside": RHINOINSIDE_AVAILABLE,
        "engine": "Rhino 8 (rhinoinside)" if RHINOINSIDE_AVAILABLE else "Mock (근사값)",
    }


@app.post("/projects")
def create_project(project_name: str = Form(...)):
    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO projects (project_name, reg_date) VALUES (?,?)",
                     (project_name, datetime.now().isoformat()))
        conn.commit()
        row = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    finally:
        conn.close()
    return {"project_id": row["id"], "project_name": project_name}


@app.get("/projects")
def list_projects():
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY reg_date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_project_data(conn, project_id: int) -> None:
    """프로젝트 및 연관 층·구역·캐시 일괄 삭제"""
    fm_rows = conn.execute("SELECT id FROM floor_models WHERE project_id=?", (project_id,)).fetchall()
    for fm in fm_rows:
        delete_preview_cache_file(fm["id"])
    conn.execute(
        """DELETE FROM actual_pours WHERE zone_id IN (
               SELECT id FROM pour_zones WHERE project_id=?
           )""",
        (project_id,),
    )
    conn.execute("DELETE FROM pour_zones WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM strength_zones WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM floor_models WHERE project_id=?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


@app.delete("/projects/{project_name}")
def delete_project(project_name: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    delete_project_data(conn, p["id"])
    conn.commit()
    conn.close()
    return {"status": "ok", "deleted": project_name}


@app.post("/floors/upload")
async def upload_floor_model(
    file: UploadFile = File(...),
    project_name: str = Form(...),
    floor_label: str = Form(...),
):
    if not file.filename.lower().endswith(".3dm"):
        raise HTTPException(400, ".3dm 파일만 지원됩니다")

    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO projects (project_name, reg_date) VALUES (?,?)",
                 (project_name, datetime.now().isoformat()))
    conn.commit()
    project = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    project_id = project["id"]

    safe_name = f"{project_name}_{floor_label}_{int(time.time())}.3dm"
    file_path = UPLOAD_DIR / safe_name
    content = await file.read()
    file_path.write_bytes(content)

    metadata = extract_model_metadata(str(file_path))
    default_strengths = get_project_strengths(conn, project_id)

    cur = conn.execute(
        """INSERT INTO floor_models (project_id, floor_label, file_path, bbox_json, object_count, reg_date)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(project_id, floor_label) DO UPDATE SET
             file_path=excluded.file_path, bbox_json=excluded.bbox_json,
             object_count=excluded.object_count, reg_date=excluded.reg_date""",
        (project_id, floor_label, str(file_path),
         json.dumps(metadata.get("bbox")), metadata.get("object_count", 0),
         datetime.now().isoformat()))
    floor_model_id = cur.lastrowid
    if floor_model_id == 0:
        row = conn.execute(
            "SELECT id FROM floor_models WHERE project_id=? AND floor_label=?",
            (project_id, floor_label),
        ).fetchone()
        floor_model_id = row["id"]

    members = sync_floor_members(conn, floor_model_id, str(file_path), default_strengths)
    build_and_store_preview_cache(
        conn, floor_model_id, metadata.get("bbox"), str(file_path), default_strengths,
        project_id, floor_label,
    )
    conn.commit()
    conn.close()

    part_summary = {}
    for m in members:
        part_summary[m["part_type"]] = part_summary.get(m["part_type"], 0) + 1

    return {
        "status": "ok",
        "project_id": project_id,
        "floor_label": floor_label,
        "bbox": metadata.get("bbox"),
        "object_count": metadata.get("object_count", 0),
        "member_count": len(members),
        "part_summary": part_summary,
        "snap_size": compute_snap_size(metadata.get("bbox")),
    }


@app.post("/floors/rename")
def rename_floor(req: dict):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (req["project_name"],)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    conn.execute("UPDATE floor_models SET floor_label=? WHERE project_id=? AND floor_label=?",
                 (req["new_label"], p["id"], req["old_label"]))
    conn.execute("UPDATE pour_zones SET floor_label=? WHERE project_id=? AND floor_label=?",
                 (req["new_label"], p["id"], req["old_label"]))
    conn.execute("UPDATE strength_zones SET floor_label=? WHERE project_id=? AND floor_label=?",
                 (req["new_label"], p["id"], req["old_label"]))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/floors/delete")
def delete_floor(req: dict):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (req["project_name"],)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    conn.execute("DELETE FROM floor_models WHERE project_id=? AND floor_label=?", (p["id"], req["floor_label"]))
    conn.execute("DELETE FROM strength_zones WHERE project_id=? AND floor_label=?", (p["id"], req["floor_label"]))
    conn.execute("""DELETE FROM actual_pours
                    WHERE zone_id IN (
                        SELECT id FROM pour_zones WHERE project_id=? AND floor_label=?
                    )""", (p["id"], req["floor_label"]))
    conn.execute("DELETE FROM pour_zones WHERE project_id=? AND floor_label=?", (p["id"], req["floor_label"]))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/floors/{project_name}")
def list_floors(project_name: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    rows = conn.execute(
        """SELECT id, floor_label, bbox_json, object_count, member_count, reg_date,
                  preview_cache_at FROM floor_models WHERE project_id=? ORDER BY floor_label""",
        (p["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _preview_gzip_response(path: Path, cache_at: Optional[str], cache_kind: str) -> Response:
    headers = {
        "Cache-Control": "private, no-cache, must-revalidate",
        "X-Preview-Cache": cache_kind,
        "X-Preview-Gzip": "1",
        "Content-Type": "application/json",
    }
    if cache_at:
        headers["ETag"] = f'"{cache_at}"'
        headers["X-Preview-Cache-At"] = cache_at
    return Response(content=path.read_bytes(), headers=headers)


@app.get("/floors/{project_name}/{floor_label}/preview")
def get_floor_preview(
    project_name: str, floor_label: str,
    rebuild: bool = Query(False),
    meta: bool = Query(False),
):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute(
        """SELECT id, file_path, bbox_json, preview_cache_file, preview_cache_at
           FROM floor_models WHERE project_id=? AND floor_label=?""",
        (p["id"], floor_label),
    ).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")

    fm_id = fm["id"]
    cache_path = _preview_cache_path(fm_id)
    raster_path = _preview_raster_path(fm_id)

    if meta:
        cached = None if rebuild else load_preview_cache_file(fm_id)
        if not cached:
            default_strengths = get_project_strengths(conn, p["id"])
            cached = build_and_store_preview_cache(
                conn, fm_id, fm["bbox_json"], fm["file_path"], default_strengths, p["id"], floor_label,
            )
            if cached:
                conn.commit()
        if not cached:
            bbox = _parse_bbox(fm["bbox_json"])
            conn.close()
            raise HTTPException(500, "2D preview 메타 생성 실패")
        row = conn.execute("SELECT preview_cache_at FROM floor_models WHERE id=?", (fm_id,)).fetchone()
        conn.close()
        return JSONResponse({
            "v": cached.get("v", PREVIEW_FMT_VERSION),
            "bbox": cached.get("bbox"),
            "snap_size": cached.get("snap_size"),
            "n": cached.get("n", 0),
            "members": cached.get("members", 0),
            "cache_at": row["preview_cache_at"] if row else None,
        }, headers={"Cache-Control": "private, max-age=120", "X-Preview-Cache": "meta"})

    if not rebuild and cache_path.is_file():
        conn.close()
        return _preview_gzip_response(cache_path, fm["preview_cache_at"], "file")

    default_strengths = get_project_strengths(conn, p["id"])
    preview = build_and_store_preview_cache(
        conn, fm_id, fm["bbox_json"], fm["file_path"], default_strengths, p["id"], floor_label,
    )
    if not preview:
        conn.close()
        raise HTTPException(500, "2D preview 생성 실패 — ↻ 부재 재추출을 실행하세요")
    conn.commit()
    row = conn.execute("SELECT preview_cache_at FROM floor_models WHERE id=?", (fm_id,)).fetchone()
    conn.close()
    if cache_path.is_file():
        return _preview_gzip_response(cache_path, row["preview_cache_at"] if row else None, "built")
    return JSONResponse(preview, headers={"X-Preview-Cache": "built"})


@app.get("/floors/{project_name}/{floor_label}/preview-raster")
def get_floor_preview_raster(project_name: str, floor_label: str, rebuild: bool = Query(False)):
    """2D 평면 사전 렌더 WebP — PileXY식 즉시 표시"""
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute(
        """SELECT id, file_path, bbox_json, preview_cache_at
           FROM floor_models WHERE project_id=? AND floor_label=?""",
        (p["id"], floor_label),
    ).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")

    fm_id = fm["id"]
    raster_path = _preview_raster_path(fm_id)
    if rebuild or not raster_path.is_file():
        cached = load_preview_cache_file(fm_id)
        if not cached:
            default_strengths = get_project_strengths(conn, p["id"])
            cached = build_and_store_preview_cache(
                conn, fm_id, fm["bbox_json"], fm["file_path"], default_strengths, p["id"], floor_label,
            )
            if cached:
                conn.commit()
        else:
            ensure_preview_raster(fm_id, cached)
    conn.close()

    if not raster_path.is_file():
        raise HTTPException(404, "2D raster 없음 — ↻ 부재 재추출 또는 warm-preview 실행")

    headers = {
        "Cache-Control": "private, max-age=86400",
        "X-Preview-Cache": "raster",
    }
    if fm["preview_cache_at"]:
        headers["ETag"] = f'"{fm["preview_cache_at"]}"'
    return FileResponse(raster_path, media_type="image/webp", headers=headers)


@app.post("/floors/{project_name}/ensure-preview-cache")
def ensure_project_preview_cache(project_name: str):
    """2D preview gzip 캐시가 없는 층만 백그라운드 일괄 생성"""
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    rows = conn.execute(
        "SELECT id, file_path, bbox_json, floor_label FROM floor_models WHERE project_id=?",
        (p["id"],),
    ).fetchall()
    default_strengths = get_project_strengths(conn, p["id"])
    warmed, skipped, raster_warmed = [], [], []
    for fm in rows:
        json_path = _preview_cache_path(fm["id"])
        raster_path = _preview_raster_path(fm["id"])
        if json_path.is_file() and raster_path.is_file():
            skipped.append(fm["floor_label"])
            continue
        if json_path.is_file() and not raster_path.is_file():
            cached = load_preview_cache_file(fm["id"])
            if ensure_preview_raster(fm["id"], cached):
                raster_warmed.append(fm["floor_label"])
            continue
        preview = build_and_store_preview_cache(
            conn, fm["id"], fm["bbox_json"], fm["file_path"], default_strengths, p["id"], fm["floor_label"],
        )
        if preview:
            warmed.append(fm["floor_label"])
    conn.commit()
    conn.close()
    return {"status": "ok", "warmed": warmed, "raster_warmed": raster_warmed, "skipped": skipped}


@app.post("/floors/{project_name}/{floor_label}/warm-preview")
def warm_floor_preview(project_name: str, floor_label: str):
    """2D preview 캐시 강제 재생성 (기존 층 일괄 전처리용)"""
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute(
        "SELECT id, file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
        (p["id"], floor_label),
    ).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")
    delete_preview_cache_file(fm["id"])
    default_strengths = get_project_strengths(conn, p["id"])
    preview = build_and_store_preview_cache(
        conn, fm["id"], fm["bbox_json"], fm["file_path"], default_strengths, p["id"], floor_label,
    )
    conn.commit()
    conn.close()
    if not preview:
        raise HTTPException(500, "2D preview 생성 실패")
    return {"status": "ok", "polygons": preview.get("n", 0), "members": preview.get("members", 0)}


@app.get("/floors/{project_name}/{floor_label}/mesh3d")
def get_floor_mesh3d(project_name: str, floor_label: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute(
        "SELECT id, file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
        (p["id"], floor_label),
    ).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")

    default_strengths = get_project_strengths(conn, p["id"])
    strength_zones = get_strength_zones_for_floor(conn, p["id"], floor_label)
    member_overrides = get_member_overrides(conn, fm["id"])
    mesh_data = extract_3d_mesh(
        fm["file_path"], default_strengths, strength_zones, member_overrides,
    )
    # DB 부재 메타(부위·강도)로 mesh 보강 — rhino3dm UserString 누락 대비
    from geometry_engine import resolve_member_strength, mesh_display_color, get_member_orientation
    rows = conn.execute(
        """SELECT object_index, part_type, orientation, rhino_value, strength_override, centroid_json
           FROM floor_members WHERE floor_model_id=?""",
        (fm["id"],),
    ).fetchall()
    member_map = {r["object_index"]: dict(r) for r in rows}
    for obj in mesh_data:
        idx = obj.get("object_index")
        dbm = member_map.get(idx)
        if not dbm:
            continue
        part = dbm.get("part_type") or obj.get("part_type") or ""
        obj["part_type"] = part
        obj["orientation"] = dbm.get("orientation") or obj.get("orientation") or get_member_orientation(part)
        centroid = json.loads(dbm["centroid_json"]) if dbm.get("centroid_json") else None
        if not centroid and obj.get("vertices"):
            verts = obj["vertices"]
            centroid = {
                "x": sum(v[0] for v in verts) / len(verts),
                "y": sum(v[1] for v in verts) / len(verts),
            }
        strength = resolve_member_strength(
            part, dbm.get("rhino_value"), centroid or {"x": 0, "y": 0},
            strength_zones, dbm.get("strength_override"), default_strengths,
        )
        obj["strength"] = strength
        obj["color"] = mesh_display_color(part, strength, obj["orientation"])
    bbox = json.loads(fm["bbox_json"]) if fm["bbox_json"] else None
    conn.close()
    return {"bbox": bbox, "objects": mesh_data}


# --- Zones ---
@app.post("/zones")
def create_zone(req: ZoneCreate):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (req.project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    now = datetime.now().isoformat()
    color = "#f59e0b" if req.status == "planned" else "#10b981"
    cur = conn.execute(
        """INSERT INTO pour_zones (project_id, floor_label, zone_name, polygon_json, status, planned_date, color, memo, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (p["id"], req.floor_label, req.zone_name, json.dumps(req.polygon),
         req.status, req.planned_date, color, req.memo, now, now))
    conn.commit()
    zone_id = cur.lastrowid
    conn.close()
    return {"status": "ok", "zone_id": zone_id}


@app.get("/zones/{project_name}")
def list_zones(project_name: str, floor_label: Optional[str] = None):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    q = "SELECT * FROM pour_zones WHERE project_id=?"
    params = [p["id"]]
    if floor_label:
        q += " AND floor_label=?"
        params.append(floor_label)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["polygon"] = json.loads(d["polygon_json"])
        del d["polygon_json"]
        d["has_intersection_mesh"] = bool(d.get("intersection_mesh_json"))
        if d.get("volume_by_strength_json"):
            d["volume_by_strength"] = json.loads(d["volume_by_strength_json"])
        else:
            d["volume_by_strength"] = {}
        if d.get("calc_breakdown_json"):
            d["calc_breakdown"] = json.loads(d["calc_breakdown_json"])
        for k in ("intersection_mesh_json", "volume_by_strength_json", "calc_breakdown_json"):
            if k in d:
                del d[k]
        result.append(d)
    return result


@app.get("/zones/{zone_id}/intersection-mesh")
def get_intersection_mesh(zone_id: int):
    conn = get_db()
    row = conn.execute("SELECT intersection_mesh_json FROM pour_zones WHERE id=?", (zone_id,)).fetchone()
    conn.close()
    if not row or not row["intersection_mesh_json"]:
        return {"mesh": []}
    return {"mesh": json.loads(row["intersection_mesh_json"])}


@app.put("/zones/{zone_id}")
def update_zone(zone_id: int, req: ZoneUpdate):
    conn = get_db()
    updates, params = [], []
    if req.zone_name is not None: updates.append("zone_name=?"); params.append(req.zone_name)
    if req.polygon is not None: updates.append("polygon_json=?"); params.append(json.dumps(req.polygon))
    if req.status is not None:
        updates.append("status=?"); params.append(req.status)
        updates.append("color=?"); params.append("#10b981" if req.status == "completed" else "#f59e0b")
    if req.actual_volume is not None: updates.append("actual_volume=?"); params.append(req.actual_volume)
    if req.planned_date is not None: updates.append("planned_date=?"); params.append(req.planned_date)
    if req.completed_date is not None: updates.append("completed_date=?"); params.append(req.completed_date)
    if req.memo is not None: updates.append("memo=?"); params.append(req.memo)
    if updates:
        updates.append("updated_at=?"); params.append(datetime.now().isoformat())
        params.append(zone_id)
        conn.execute(f"UPDATE pour_zones SET {','.join(updates)} WHERE id=?", params)
        conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/zones/{zone_id}")
def delete_zone(zone_id: int):
    conn = get_db()
    conn.execute("DELETE FROM actual_pours WHERE zone_id=?", (zone_id,))
    conn.execute("DELETE FROM pour_zones WHERE id=?", (zone_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# --- Actual Pours (실제 타설 범위) ---
@app.post("/actual-pours")
def create_actual_pour(req: ActualPourCreate):
    conn = get_db()
    zone = conn.execute("SELECT id FROM pour_zones WHERE id=?", (req.zone_id,)).fetchone()
    if not zone:
        conn.close()
        raise HTTPException(404, "구역 없음")
    conn.execute("""INSERT INTO actual_pours (zone_id, pour_label, polygon_json, pour_date, memo, created_at)
                    VALUES (?,?,?,?,?,?)""",
                 (req.zone_id, req.pour_label, json.dumps(req.polygon),
                  req.pour_date or date.today().isoformat(), req.memo, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/actual-pours/{zone_id}")
def list_actual_pours(zone_id: int):
    conn = get_db()
    rows = conn.execute("SELECT * FROM actual_pours WHERE zone_id=? ORDER BY created_at ASC", (zone_id,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["polygon"] = json.loads(d["polygon_json"])
        del d["polygon_json"]
        if d.get("volume_by_strength_json"):
            d["volume_by_strength"] = json.loads(d["volume_by_strength_json"])
        else:
            d["volume_by_strength"] = {}
        del d["volume_by_strength_json"]
        d["has_intersection_mesh"] = bool(d.get("intersection_mesh_json"))
        if d.get("intersection_mesh_json"):
            d["intersection_mesh"] = json.loads(d["intersection_mesh_json"])
        else:
            d["intersection_mesh"] = []
        del d["intersection_mesh_json"]
        result.append(d)
    return result

@app.put("/actual-pours/{pour_id}")
def update_actual_pour(pour_id: int, req: ActualPourUpdate):
    conn = get_db()
    updates, params = [], []
    if req.pour_label is not None: updates.append("pour_label=?"); params.append(req.pour_label)
    if req.polygon is not None: updates.append("polygon_json=?"); params.append(json.dumps(req.polygon))
    if req.pour_date is not None: updates.append("pour_date=?"); params.append(req.pour_date)
    if req.volume is not None: updates.append("volume=?"); params.append(req.volume)
    if req.memo is not None: updates.append("memo=?"); params.append(req.memo)
    if updates:
        params.append(pour_id)
        conn.execute(f"UPDATE actual_pours SET {','.join(updates)} WHERE id=?", params)
        conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/actual-pours/{pour_id}")
def delete_actual_pour(pour_id: int):
    conn = get_db()
    conn.execute("DELETE FROM actual_pours WHERE id=?", (pour_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/actual-pours/remaining/{zone_id}")
def get_remaining_polygon(zone_id: int):
    conn = get_db()
    zone = conn.execute("SELECT polygon_json FROM pour_zones WHERE id=?", (zone_id,)).fetchone()
    if not zone:
        conn.close()
        raise HTTPException(404, "구역 없음")
    pours = conn.execute("SELECT polygon_json FROM actual_pours WHERE zone_id=?", (zone_id,)).fetchall()
    conn.close()
    plan_polygon = json.loads(zone["polygon_json"])
    poured_polygons = [json.loads(r["polygon_json"]) for r in pours]
    remain = compute_remaining_polygon(plan_polygon, poured_polygons)
    return {"polygon": remain}


@app.post("/actual-pours/{pour_id}/calculate")
def calculate_actual_pour(pour_id: int):
    conn = get_db()
    row = conn.execute("""
        SELECT ap.id, ap.polygon_json, z.project_id, z.floor_label
        FROM actual_pours ap
        JOIN pour_zones z ON z.id = ap.zone_id
        WHERE ap.id=?
    """, (pour_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "타설 기록 없음")
    fm = conn.execute("SELECT file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
                      (row["project_id"], row["floor_label"])).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")

    fm_id_row = conn.execute("SELECT id FROM floor_models WHERE project_id=? AND floor_label=?",
                             (row["project_id"], row["floor_label"])).fetchone()

    polygon = json.loads(row["polygon_json"])
    bbox = json.loads(fm["bbox_json"]) if fm["bbox_json"] else None
    z_min = bbox["min"]["z"] if bbox else 0
    z_max = bbox["max"]["z"] if bbox else 0
    result = calculate_zone_volume_breakdown(
        fm["file_path"], polygon, z_min, z_max,
        get_strength_zones_for_floor(conn, row["project_id"], row["floor_label"]),
        get_member_overrides(conn, fm_id_row["id"]),
        get_project_strengths(conn, row["project_id"]),
    )
    volume = float(result.get("volume", 0.0))
    by_strength = result.get("by_strength", {})
    intersection_mesh = result.get("intersection_mesh", [])
    conn.execute(
        """UPDATE actual_pours SET volume=?, volume_by_strength_json=?, intersection_mesh_json=?
           WHERE id=?""",
        (
            volume,
            json.dumps(by_strength, ensure_ascii=False),
            json.dumps(intersection_mesh, ensure_ascii=False) if intersection_mesh else None,
            pour_id,
        ),
    )
    conn.commit()
    conn.close()
    return {
        "status": result.get("status", "ok"),
        "volume": volume,
        "volume_horizontal": result.get("volume_horizontal", 0),
        "volume_vertical": result.get("volume_vertical", 0),
        "by_strength": result.get("by_strength", {}),
        "intersection_mesh": intersection_mesh,
        "trucks": trucks_from_volume(volume),
        "warnings": result.get("warnings", []),
    }


# --- Calculate (Background Task + Status) ---
calc_jobs = {}  # {job_id: {status, progress, message, result, start_time}}

def _run_calculation(job_id: str, file_path: str, polygon: list, z_min: float, z_max: float,
                     zone_id: int, strength_zones: list, member_overrides: dict, default_strengths: dict):
    """백그라운드 스레드에서 부재별 체적 계산 실행"""
    calc_jobs[job_id]["status"] = "running"
    calc_jobs[job_id]["message"] = "모델 파일 로딩 중..."
    calc_jobs[job_id]["progress"] = 10

    t0 = time.time()
    try:
        calc_jobs[job_id]["message"] = "부재(수평/수직) 분석 중..."
        calc_jobs[job_id]["progress"] = 20

        time.sleep(0.1)
        calc_jobs[job_id]["message"] = "강도별 Boolean Intersection 수행 중..."
        calc_jobs[job_id]["progress"] = 40

        result = calculate_zone_volume_breakdown(
            file_path, polygon, z_min, z_max,
            strength_zones, member_overrides, default_strengths,
        )
        elapsed = round(time.time() - t0, 3)

        calc_jobs[job_id]["message"] = "체적 계산 완료, DB 저장 중..."
        calc_jobs[job_id]["progress"] = 90

        conn = get_db()
        _persist_zone_calculation(conn, zone_id, result)
        conn.commit()
        conn.close()

        calc_jobs[job_id]["status"] = "completed"
        calc_jobs[job_id]["progress"] = 100
        h = result.get("volume_horizontal", 0)
        v = result.get("volume_vertical", 0)
        calc_jobs[job_id]["message"] = f"완료! {result['volume']:.4f} m³ (수평 {h:.2f} / 수직 {v:.2f})"
        calc_jobs[job_id]["result"] = {
            "status": result["status"], "volume": result["volume"], "unit": "m³",
            "volume_horizontal": h, "volume_vertical": v,
            "by_strength": result.get("by_strength", {}),
            "by_part": result.get("by_part", {}),
            "elapsed_time": elapsed, "object_count": result.get("object_count", 0),
            "warnings": result.get("warnings", []),
            "intersection_mesh": result.get("intersection_mesh", []),
        }
    except Exception as e:
        calc_jobs[job_id]["status"] = "error"
        calc_jobs[job_id]["progress"] = 0
        calc_jobs[job_id]["message"] = f"오류: {str(e)}"
        calc_jobs[job_id]["result"] = {"status": "error", "volume": 0, "warnings": [str(e)]}


def _persist_zone_calculation(conn, zone_id: int, result: dict) -> None:
    intersection_mesh_data = result.get("intersection_mesh", [])
    conn.execute(
        """UPDATE pour_zones SET
           calculated_volume=?, volume_horizontal=?, volume_vertical=?,
           volume_by_strength_json=?, calc_breakdown_json=?,
           intersection_mesh_json=?, updated_at=? WHERE id=?""",
        (
            result.get("volume", 0),
            result.get("volume_horizontal", 0),
            result.get("volume_vertical", 0),
            json.dumps(result.get("by_strength", {}), ensure_ascii=False),
            json.dumps({
                "by_part": result.get("by_part", {}),
                "members_in_zone": result.get("members_in_zone", []),
            }, ensure_ascii=False),
            json.dumps(intersection_mesh_data) if intersection_mesh_data else None,
            datetime.now().isoformat(),
            zone_id,
        ),
    )


@app.post("/floors/{project_name}/{floor_label}/recalculate-zones")
def recalculate_floor_zones(project_name: str, floor_label: str):
    """강도구역 변경 등 — 해당 층 타설구역 물량 일괄 재산출"""
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute(
        "SELECT id, file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
        (p["id"], floor_label),
    ).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")

    default_strengths = get_project_strengths(conn, p["id"])
    strength_zones = get_strength_zones_for_floor(conn, p["id"], floor_label)
    member_overrides = get_member_overrides(conn, fm["id"])
    bbox = json.loads(fm["bbox_json"]) if fm["bbox_json"] else None
    z_min = bbox["min"]["z"] if bbox else 0
    z_max = bbox["max"]["z"] if bbox else 0

    rows = conn.execute(
        "SELECT id, polygon_json FROM pour_zones WHERE project_id=? AND floor_label=?",
        (p["id"], floor_label),
    ).fetchall()
    updated = []
    for row in rows:
        polygon = json.loads(row["polygon_json"])
        result = calculate_zone_volume_breakdown(
            fm["file_path"], polygon, z_min, z_max,
            strength_zones, member_overrides, default_strengths,
        )
        _persist_zone_calculation(conn, row["id"], result)
        updated.append({
            "zone_id": row["id"],
            "volume": result.get("volume", 0),
            "by_strength": result.get("by_strength", {}),
        })
    conn.commit()
    conn.close()
    return {"status": "ok", "recalculated": len(updated), "zones": updated}


@app.post("/calculate")
def calculate(req: CalcRequest):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (req.project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    zone = conn.execute("SELECT * FROM pour_zones WHERE id=?", (req.zone_id,)).fetchone()
    if not zone:
        conn.close()
        raise HTTPException(404, "구역 없음")
    fm = conn.execute("SELECT id, file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
                      (p["id"], zone["floor_label"])).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델이 업로드되지 않았습니다")

    default_strengths = get_project_strengths(conn, p["id"])
    strength_zones = get_strength_zones_for_floor(conn, p["id"], zone["floor_label"])
    member_overrides = get_member_overrides(conn, fm["id"])
    conn.close()

    polygon = json.loads(zone["polygon_json"])
    bbox = json.loads(fm["bbox_json"]) if fm["bbox_json"] else None
    z_min = bbox["min"]["z"] if bbox else 0
    z_max = bbox["max"]["z"] if bbox else 0

    job_id = str(uuid.uuid4())[:8]
    calc_jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "계산 대기 중...",
        "result": None,
        "start_time": time.time(),
    }

    thread = threading.Thread(
        target=_run_calculation,
        args=(job_id, fm["file_path"], polygon, z_min, z_max, req.zone_id,
              strength_zones, member_overrides, default_strengths),
    )
    thread.daemon = True
    thread.start()

    return {"job_id": job_id, "status": "queued", "engine": "Rhino 8" if RHINOINSIDE_AVAILABLE else "Mock"}


@app.get("/calculate/status/{job_id}")
def calc_status(job_id: str):
    if job_id not in calc_jobs:
        raise HTTPException(404, "작업 없음")
    job = calc_jobs[job_id]
    elapsed = round(time.time() - job["start_time"], 1)
    return {
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "elapsed": elapsed,
        "result": job["result"],
    }


# --- Project default strengths ---
@app.get("/projects/{project_name}/strengths")
def get_project_strengths_api(project_name: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    strengths = get_project_strengths(conn, p["id"])
    conn.close()
    return {"default_strengths": strengths}


@app.put("/projects/{project_name}/strengths")
def update_project_strengths(project_name: str, req: ProjectStrengthsUpdate):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    conn.execute(
        "UPDATE projects SET default_strengths_json=? WHERE id=?",
        (json.dumps(req.default_strengths, ensure_ascii=False), p["id"]),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "default_strengths": req.default_strengths}


# --- Strength zones (평면 강도 지정 구역) ---
@app.get("/strength-zones/{project_name}")
def list_strength_zones(project_name: str, floor_label: Optional[str] = None):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    if floor_label:
        zones = get_strength_zones_for_floor(conn, p["id"], floor_label)
    else:
        rows = conn.execute(
            "SELECT * FROM strength_zones WHERE project_id=? ORDER BY floor_label, created_at",
            (p["id"],),
        ).fetchall()
        zones = []
        for r in rows:
            d = dict(r)
            d["polygon"] = json.loads(d["polygon_json"])
            d["target_parts"] = json.loads(d["target_parts_json"]) if d.get("target_parts_json") else ["기둥", "벽체"]
            del d["polygon_json"]
            del d["target_parts_json"]
            zones.append(d)
    conn.close()
    return zones


@app.post("/strength-zones")
def create_strength_zone(req: StrengthZoneCreate):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (req.project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    now = datetime.now().isoformat()
    cur = conn.execute(
        """INSERT INTO strength_zones
           (project_id, floor_label, zone_name, polygon_json, strength, target_parts_json, color, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (p["id"], req.floor_label, req.zone_name, json.dumps(req.polygon),
         req.strength, json.dumps(req.target_parts, ensure_ascii=False),
         "#ef4444", now, now),
    )
    conn.commit()
    zone_id = cur.lastrowid
    conn.close()
    return {"status": "ok", "zone_id": zone_id}


@app.put("/strength-zones/{zone_id}")
def update_strength_zone(zone_id: int, req: StrengthZoneUpdate):
    conn = get_db()
    updates, params = [], []
    if req.zone_name is not None:
        updates.append("zone_name=?"); params.append(req.zone_name)
    if req.polygon is not None:
        updates.append("polygon_json=?"); params.append(json.dumps(req.polygon))
    if req.strength is not None:
        updates.append("strength=?"); params.append(req.strength)
    if req.target_parts is not None:
        updates.append("target_parts_json=?"); params.append(json.dumps(req.target_parts, ensure_ascii=False))
    if updates:
        updates.append("updated_at=?"); params.append(datetime.now().isoformat())
        params.append(zone_id)
        conn.execute(f"UPDATE strength_zones SET {','.join(updates)} WHERE id=?", params)
        conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/strength-zones/{zone_id}")
def delete_strength_zone(zone_id: int):
    conn = get_db()
    conn.execute("DELETE FROM strength_zones WHERE id=?", (zone_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# --- Floor members ---
@app.get("/floors/{project_name}/{floor_label}/members")
def list_floor_members(project_name: str, floor_label: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute(
        "SELECT id, file_path FROM floor_models WHERE project_id=? AND floor_label=?",
        (p["id"], floor_label),
    ).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")
    default_strengths = get_project_strengths(conn, p["id"])
    strength_zones = get_strength_zones_for_floor(conn, p["id"], floor_label)
    overrides = get_member_overrides(conn, fm["id"])
    rows = conn.execute(
        "SELECT * FROM floor_members WHERE floor_model_id=? ORDER BY part_type, object_index",
        (fm["id"],),
    ).fetchall()
    conn.close()

    if not rows:
        members = extract_floor_members(fm["file_path"], default_strengths)
        result = []
        for m in members:
            eff = resolve_effective_strength(m, overrides, strength_zones, default_strengths)
            result.append({**m, "id": None, "effective_strength": eff})
        return result

    from geometry_engine import resolve_member_strength as _resolve
    result = []
    for r in rows:
        d = dict(r)
        centroid = json.loads(d["centroid_json"]) if d.get("centroid_json") else {"x": 0, "y": 0}
        eff = _resolve(
            d["part_type"], d.get("rhino_value"), centroid,
            strength_zones, d.get("strength_override"), default_strengths,
        )
        d["effective_strength"] = eff
        d["centroid"] = centroid
        if d.get("bbox_json"):
            d["bbox"] = json.loads(d["bbox_json"])
        for k in ("centroid_json", "bbox_json"):
            if k in d:
                del d[k]
        result.append(d)
    return result


@app.put("/members/{member_id}")
def update_member_strength(member_id: int, req: MemberStrengthUpdate):
    conn = get_db()
    conn.execute(
        "UPDATE floor_members SET strength_override=? WHERE id=?",
        (req.strength_override, member_id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/floors/{project_name}/{floor_label}/resync-members")
def resync_floor_members(project_name: str, floor_label: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute(
        "SELECT id, file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
        (p["id"], floor_label),
    ).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델 없음")
    default_strengths = get_project_strengths(conn, p["id"])
    members = sync_floor_members(conn, fm["id"], fm["file_path"], default_strengths)
    delete_preview_cache_file(fm["id"])
    build_and_store_preview_cache(
        conn, fm["id"], fm["bbox_json"], fm["file_path"], default_strengths, p["id"], floor_label,
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "member_count": len(members)}


def resolve_effective_strength(member, overrides, strength_zones, default_strengths):
    from geometry_engine import resolve_member_strength
    return resolve_member_strength(
        member["part_type"], member.get("rhino_value"), member.get("centroid", {"x": 0, "y": 0}),
        strength_zones, overrides.get(member["object_index"]), default_strengths,
    )


@app.get("/summary/{project_name}")
def summary(project_name: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    rows = conn.execute(
        "SELECT status, COUNT(*) cnt, SUM(calculated_volume) calc, SUM(actual_volume) actual FROM pour_zones WHERE project_id=? GROUP BY status",
        (p["id"],)).fetchall()
    conn.close()
    s = {"planned": {"count":0,"calc_volume":0,"actual_volume":0}, "completed": {"count":0,"calc_volume":0,"actual_volume":0}}
    for r in rows:
        if r["status"] in s:
            s[r["status"]] = {"count": r["cnt"], "calc_volume": r["calc"] or 0, "actual_volume": r["actual"] or 0}
    return s


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=9000, reload=False)
