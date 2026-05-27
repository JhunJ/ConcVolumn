"""
ConCast - 콘크리트 타설 물량 관리 시스템
Concrete Casting Volume Management
"""
import os, json, sqlite3, time, threading, uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from geometry_engine import (
    extract_model_metadata,
    extract_preview_geometry,
    extract_3d_mesh,
    calculate_polygon_intersection_volume,
    compute_remaining_polygon,
    compute_snap_size,
    RHINOINSIDE_AVAILABLE,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "concast.db"

app = FastAPI(title="ConCast - 콘크리트 타설 물량 관리", version="3.0.0")


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
            reg_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS floor_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            floor_label TEXT NOT NULL,
            file_path TEXT NOT NULL,
            bbox_json TEXT,
            object_count INTEGER DEFAULT 0,
            reg_date TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id),
            UNIQUE(project_id, floor_label)
        );
        CREATE TABLE IF NOT EXISTS pour_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            floor_label TEXT NOT NULL,
            zone_name TEXT NOT NULL,
            polygon_json TEXT NOT NULL,
            status TEXT DEFAULT 'planned',
            calculated_volume REAL DEFAULT 0,
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
            memo TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (zone_id) REFERENCES pour_zones(id) ON DELETE CASCADE
        );
    """)
    # 기존 DB 마이그레이션: intersection_mesh_json 컬럼 추가
    try:
        conn.execute("ALTER TABLE pour_zones ADD COLUMN intersection_mesh_json TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

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

    conn.execute(
        """INSERT INTO floor_models (project_id, floor_label, file_path, bbox_json, object_count, reg_date)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(project_id, floor_label) DO UPDATE SET
             file_path=excluded.file_path, bbox_json=excluded.bbox_json,
             object_count=excluded.object_count, reg_date=excluded.reg_date""",
        (project_id, floor_label, str(file_path),
         json.dumps(metadata.get("bbox")), metadata.get("object_count", 0),
         datetime.now().isoformat()))
    conn.commit()
    conn.close()

    return {
        "status": "ok",
        "project_id": project_id,
        "floor_label": floor_label,
        "bbox": metadata.get("bbox"),
        "object_count": metadata.get("object_count", 0),
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
        "SELECT id, floor_label, bbox_json, object_count, reg_date FROM floor_models WHERE project_id=? ORDER BY floor_label",
        (p["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/floors/{project_name}/{floor_label}/preview")
def get_floor_preview(project_name: str, floor_label: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute("SELECT file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
                      (p["id"], floor_label)).fetchone()
    conn.close()
    if not fm:
        raise HTTPException(404, "해당 층 모델 없음")

    preview = extract_preview_geometry(fm["file_path"])
    bbox = json.loads(fm["bbox_json"]) if fm["bbox_json"] else None
    return {"bbox": bbox, "edges": preview.get("edges", []), "meshes": preview.get("meshes", []),
            "snap_size": compute_snap_size(bbox)}


@app.get("/floors/{project_name}/{floor_label}/mesh3d")
def get_floor_mesh3d(project_name: str, floor_label: str):
    conn = get_db()
    p = conn.execute("SELECT id FROM projects WHERE project_name=?", (project_name,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(404, "프로젝트 없음")
    fm = conn.execute("SELECT file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
                      (p["id"], floor_label)).fetchone()
    conn.close()
    if not fm:
        raise HTTPException(404, "해당 층 모델 없음")

    mesh_data = extract_3d_mesh(fm["file_path"])
    bbox = json.loads(fm["bbox_json"]) if fm["bbox_json"] else None
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
        if "intersection_mesh_json" in d:
            del d["intersection_mesh_json"]
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

    polygon = json.loads(row["polygon_json"])
    bbox = json.loads(fm["bbox_json"]) if fm["bbox_json"] else None
    z_min = bbox["min"]["z"] if bbox else 0
    z_max = bbox["max"]["z"] if bbox else 0
    result = calculate_polygon_intersection_volume(fm["file_path"], polygon, z_min, z_max)
    volume = float(result.get("volume", 0.0))
    conn.execute("UPDATE actual_pours SET volume=? WHERE id=?", (volume, pour_id))
    conn.commit()
    conn.close()
    return {
        "status": result.get("status", "ok"),
        "volume": volume,
        "trucks": int((volume / 6.0) + 0.9999) if volume > 0 else 0,
        "warnings": result.get("warnings", []),
    }


# --- Calculate (Background Task + Status) ---
calc_jobs = {}  # {job_id: {status, progress, message, result, start_time}}

def _run_calculation(job_id: str, file_path: str, polygon: list, z_min: float, z_max: float, zone_id: int):
    """백그라운드 스레드에서 체적 계산 실행"""
    calc_jobs[job_id]["status"] = "running"
    calc_jobs[job_id]["message"] = "모델 파일 로딩 중..."
    calc_jobs[job_id]["progress"] = 10

    t0 = time.time()
    try:
        calc_jobs[job_id]["message"] = "Brep 객체 분석 중..."
        calc_jobs[job_id]["progress"] = 20

        time.sleep(0.1)
        calc_jobs[job_id]["message"] = "Boolean Intersection 수행 중... (Rhino 8 엔진)"
        calc_jobs[job_id]["progress"] = 40

        result = calculate_polygon_intersection_volume(file_path, polygon, z_min, z_max)
        elapsed = round(time.time() - t0, 3)

        calc_jobs[job_id]["message"] = "체적 계산 완료, DB 저장 중..."
        calc_jobs[job_id]["progress"] = 90

        intersection_mesh_data = result.get("intersection_mesh", [])

        conn = get_db()
        conn.execute("UPDATE pour_zones SET calculated_volume=?, intersection_mesh_json=?, updated_at=? WHERE id=?",
                     (result["volume"], json.dumps(intersection_mesh_data) if intersection_mesh_data else None,
                      datetime.now().isoformat(), zone_id))
        conn.commit()
        conn.close()

        calc_jobs[job_id]["status"] = "completed"
        calc_jobs[job_id]["progress"] = 100
        calc_jobs[job_id]["message"] = f"완료! {result['volume']:.4f} m³"
        calc_jobs[job_id]["result"] = {
            "status": result["status"], "volume": result["volume"], "unit": "m³",
            "elapsed_time": elapsed, "object_count": result.get("object_count", 0),
            "warnings": result.get("warnings", []),
            "intersection_mesh": result.get("intersection_mesh", []),
        }
    except Exception as e:
        calc_jobs[job_id]["status"] = "error"
        calc_jobs[job_id]["progress"] = 0
        calc_jobs[job_id]["message"] = f"오류: {str(e)}"
        calc_jobs[job_id]["result"] = {"status": "error", "volume": 0, "warnings": [str(e)]}


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
    fm = conn.execute("SELECT file_path, bbox_json FROM floor_models WHERE project_id=? AND floor_label=?",
                      (p["id"], zone["floor_label"])).fetchone()
    if not fm:
        conn.close()
        raise HTTPException(404, "해당 층 모델이 업로드되지 않았습니다")
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

    thread = threading.Thread(target=_run_calculation,
                              args=(job_id, fm["file_path"], polygon, z_min, z_max, req.zone_id))
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
