"""
ConCast Geometry Engine - rhinoinside 기반 정확한 Boolean Intersection 체적 계산
- rhinoinside: Python에서 직접 RhinoCommon 호출 (별도 서버 불필요)
- rhino3dm: .3dm 파일 읽기, BBox, 프리뷰 메시 추출 (경량)
- Fallback mock: rhinoinside 로딩 실패 시 근사값
"""

import math
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# rhinoinside 로딩 (앱 시작 시 1회)
# ---------------------------------------------------------------------------
RHINOINSIDE_AVAILABLE = False
RhinoGeometry = None

try:
    import rhinoinside
    rhinoinside.load(r'C:\Program Files\Rhino 8\System')
    import Rhino
    from Rhino.Geometry import (
        Point3d, Vector3d, Polyline, Surface, Brep,
        VolumeMassProperties, PolylineCurve
    )
    from System.Collections.Generic import List as NetList
    RHINOINSIDE_AVAILABLE = True
    logger.info(f"rhinoinside loaded: Rhino {Rhino.RhinoApp.Version}")
except Exception as e:
    logger.warning(f"rhinoinside 로딩 실패: {e}. Mock 모드로 동작합니다.")

# ---------------------------------------------------------------------------
# rhino3dm (파일 읽기 + 프리뷰용)
# ---------------------------------------------------------------------------
try:
    import rhino3dm
    RHINO3DM_AVAILABLE = True
except ImportError:
    RHINO3DM_AVAILABLE = False
    logger.warning("rhino3dm not installed.")


# ---------------------------------------------------------------------------
# Model Metadata Extraction
# ---------------------------------------------------------------------------

def extract_model_metadata(file_path: str) -> dict:
    """3dm 파일에서 BBox와 객체 수 추출"""
    if not RHINO3DM_AVAILABLE:
        return _mock_metadata()

    try:
        model = rhino3dm.File3dm.Read(file_path)
        if model is None:
            return _mock_metadata()

        all_min = [float("inf")] * 3
        all_max = [float("-inf")] * 3
        obj_count = 0

        for obj in model.Objects:
            geo = obj.Geometry
            if geo is None:
                continue
            bbox = geo.GetBoundingBox()
            if bbox is None:
                continue
            obj_count += 1
            mn = bbox.Min
            mx = bbox.Max
            all_min[0] = min(all_min[0], mn.X)
            all_min[1] = min(all_min[1], mn.Y)
            all_min[2] = min(all_min[2], mn.Z)
            all_max[0] = max(all_max[0], mx.X)
            all_max[1] = max(all_max[1], mx.Y)
            all_max[2] = max(all_max[2], mx.Z)

        if obj_count == 0:
            return _mock_metadata()

        return {
            "bbox": {
                "min": {"x": all_min[0], "y": all_min[1], "z": all_min[2]},
                "max": {"x": all_max[0], "y": all_max[1], "z": all_max[2]},
            },
            "object_count": obj_count,
        }
    except Exception as e:
        logger.error(f"Error extracting metadata: {e}")
        return _mock_metadata()


def _mock_metadata() -> dict:
    return {
        "bbox": {
            "min": {"x": -50.0, "y": -30.0, "z": -10.5},
            "max": {"x": 50.0, "y": 30.0, "z": 0.0},
        },
        "object_count": 0,
    }


# ---------------------------------------------------------------------------
# Preview Geometry Extraction (2D용 - 부재별 plan curve, 경량)
# ---------------------------------------------------------------------------

PART_PLAN_STYLE = {
    "수평": {"fill": "rgba(6,182,212,0.06)", "stroke": "rgba(34,211,238,0.55)", "width": 0.85, "stroke_only": True},
    "벽체": {"fill": "rgba(59,130,246,0.10)", "stroke": "rgba(59,130,246,0.45)", "width": 0.6, "stroke_only": False},
    "기둥": {"fill": "rgba(168,85,247,0.14)", "stroke": "rgba(168,85,247,0.55)", "width": 0.7, "stroke_only": True},
}

MAX_DISPLAY_PTS = 48
HORIZONTAL_MAX_PTS = 256


def _close_poly_xy(pts: List[List[float]]) -> List[List[float]]:
    if len(pts) < 3:
        return pts
    if pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]:
        return pts + [[pts[0][0], pts[0][1]]]
    return pts


def _simplify_polyline_xy(pts: List[List[float]], max_pts: int = MAX_DISPLAY_PTS) -> List[List[float]]:
    if len(pts) <= max_pts:
        return _close_poly_xy(pts)
    step = max(1, len(pts) // max_pts)
    out = [pts[i] for i in range(0, len(pts), step)]
    return _close_poly_xy(out)


def _simplify_plan_curves(curves: List[List[List[float]]], part_type: str) -> List[List[List[float]]]:
    part = normalize_part_type(part_type)
    if part in ("수평", "벽체", "기둥"):
        return [_close_poly_xy(poly) for poly in curves if poly and len(poly) >= 3]
    max_pts = MAX_DISPLAY_PTS
    return [_simplify_polyline_xy(poly, max_pts) for poly in curves if poly and len(poly) >= 3]


def _poly_area_xy(poly: List[List[float]]) -> float:
    if len(poly) < 3:
        return 0.0
    pts = poly
    if len(poly) > 3 and poly[0][0] == poly[-1][0] and poly[0][1] == poly[-1][1]:
        pts = poly[:-1]
    area = 0.0
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        area += pts[i][0] * pts[j][1] - pts[j][0] * pts[i][1]
    return abs(area) / 2.0


def _curve_to_xy_polylines(
    crv, tolerance: float = 50.0, max_z_span: Optional[float] = None,
) -> List[List[List[float]]]:
    """Rhino Curve → XY 폴리라인 (수평면 curve만 허용)"""
    if crv is None:
        return []
    z_limit = max_z_span if max_z_span is not None else max(tolerance * 3, 25.0)

    def _flatten_pts3d(pts3d: List[List[float]]) -> Optional[List[List[float]]]:
        if len(pts3d) < 3:
            return None
        zs = [p[2] for p in pts3d]
        if max(zs) - min(zs) > z_limit:
            return None
        return _close_poly_xy([[p[0], p[1]] for p in pts3d])

    try:
        ok, pline = crv.TryGetPolyline()
        if ok and pline is not None and pline.Count >= 3:
            pts3d = [[float(pline[i].X), float(pline[i].Y), float(pline[i].Z)] for i in range(pline.Count)]
            flat = _flatten_pts3d(pts3d)
            if flat:
                return [flat]
    except Exception:
        pass
    if RHINOINSIDE_AVAILABLE:
        try:
            pline_curve = crv.ToPolyline(tolerance, math.radians(8), 0, 0, 0)
            if pline_curve is not None:
                ok, pl = pline_curve.TryGetPolyline()
                if ok and pl is not None and pl.Count >= 3:
                    pts3d = [[float(pl[i].X), float(pl[i].Y), float(pl[i].Z)] for i in range(pl.Count)]
                    flat = _flatten_pts3d(pts3d)
                    if flat:
                        return [flat]
        except Exception:
            pass
        try:
            length = crv.GetLength()
            segments = max(32, min(512, int(length / max(tolerance, 1.0))))
            dom = crv.Domain
            pts3d = []
            for i in range(segments + 1):
                t = dom.Min + (dom.Max - dom.Min) * i / segments
                p = crv.PointAt(t)
                pts3d.append([float(p.X), float(p.Y), float(p.Z)])
            flat = _flatten_pts3d(pts3d)
            if flat:
                return [flat]
        except Exception:
            pass
    return []


def _section_polys_at_z(brep, z_level: float, tolerance: float, max_z_span: float) -> List[List[List[float]]]:
    """Brep을 수평면으로 절단해 2D 폴리라인 목록 반환"""
    if not RHINOINSIDE_AVAILABLE:
        return []
    try:
        from Rhino.Geometry import Plane, Point3d, Vector3d
        from Rhino.Geometry.Intersect import Intersection

        plane = Plane(Point3d(0, 0, z_level), Vector3d(0, 0, 1))
        min_area = max(tolerance * tolerance * 0.1, 10.0)
        polys: List[List[List[float]]] = []
        for tol_mult in (1.0, 2.0, 4.0, 8.0):
            ok, curves, _ = Intersection.BrepPlane(brep, plane, tolerance * tol_mult)
            if not ok or not curves:
                continue
            polys = []
            for crv in curves:
                if crv is None:
                    continue
                try:
                    if not crv.IsClosed:
                        continue
                except Exception:
                    pass
                for poly in _curve_to_xy_polylines(crv, tolerance * tol_mult, max_z_span):
                    if poly and len(poly) >= 3 and _poly_area_xy(poly) > min_area:
                        polys.append(poly)
            if polys:
                return polys
        return polys
    except Exception as e:
        logger.debug(f"BrepPlane section failed: {e}")
        return []


def _face_is_horizontal(face, min_normal_z: float = 0.85) -> bool:
    try:
        ok, pl = face.TryGetPlane()
        if ok:
            return abs(pl.ZAxis.Z) >= min_normal_z
    except Exception:
        pass
    return False


def _face_loops_to_polys(face, tess_tol: float, max_z_span: float, include_inner: bool) -> List[List[List[float]]]:
    polys: List[List[List[float]]] = []
    for li in range(face.Loops.Count):
        loop = face.Loops[li]
        try:
            from Rhino.Geometry import BrepLoopType
            if not include_inner and loop.LoopType != BrepLoopType.Outer:
                continue
        except Exception:
            if not include_inner and li > 0:
                continue
        crv = loop.To3dCurve()
        if crv is None:
            continue
        for poly in _curve_to_xy_polylines(crv, tess_tol, max_z_span):
            if poly and len(poly) >= 3:
                polys.append(poly)
    return polys


def _horizontal_faces_at_z(brep, z_target: float, z_band: float, tess_tol: float, max_z_span: float,
                           include_inner: bool, pick_largest: bool) -> List[List[List[float]]]:
    """목표 Z 근처 수평 face에서 plan curve 추출"""
    from Rhino.Geometry import AreaMassProperties

    min_face_area = max(tess_tol * tess_tol * 0.25, 25.0)
    matched: List[tuple] = []
    for i in range(brep.Faces.Count):
        face = brep.Faces[i]
        if not _face_is_horizontal(face):
            continue
        amp = AreaMassProperties.Compute(face)
        if amp is None or amp.Area < min_face_area:
            continue
        if abs(amp.Centroid.Z - z_target) > z_band:
            continue
        for poly in _face_loops_to_polys(face, tess_tol, max_z_span, include_inner):
            if poly and _poly_area_xy(poly) >= min_face_area * 0.5:
                matched.append((_poly_area_xy(poly), poly))
    if not matched:
        return []
    if pick_largest:
        return [max(matched, key=lambda x: x[0])[1]]
    matched.sort(key=lambda x: -x[0])
    return [p for _, p in matched]


def _bbox_plan_rectangle(bbox_dict: dict) -> List[List[List[float]]]:
    mn, mx = bbox_dict["min"], bbox_dict["max"]
    pts = [
        [mn["x"], mn["y"]], [mx["x"], mn["y"]],
        [mx["x"], mx["y"]], [mn["x"], mx["y"]], [mn["x"], mn["y"]],
    ]
    return [pts]


def extract_brep_plan_curves(brep, part_type: str, bbox_dict: Optional[dict] = None) -> List[List[List[float]]]:
    """
    2D 평면 표현용 외곽 curve
    - 기둥·벽체: 밑면(bottom, min Z) 외곽
    - 수평: 상부면(top, max Z) 외곽
    """
    use_top = normalize_part_type(part_type) == "수평"

    if RHINOINSIDE_AVAILABLE:
        try:
            curves = _plan_curves_rhinoinside(brep, use_top)
            if curves:
                return curves
        except Exception as e:
            logger.debug(f"plan curve rhinoinside fallback: {e}")

    return []


def _plan_curves_rhinoinside(brep, use_top: bool) -> List[List[List[float]]]:
    bbox = brep.GetBoundingBox(True)
    mn, mx = bbox.Min, bbox.Max
    z_tgt = mx.Z if use_top else mn.Z
    z_span = abs(mx.Z - mn.Z)
    z_band = max(z_span * 0.02, 5.0)
    diag = math.sqrt((mx.X - mn.X) ** 2 + (mx.Y - mn.Y) ** 2 + max(z_span, 1.0) ** 2)
    tess_tol = max(diag * 0.0003, 15.0)
    max_z_span = max(tess_tol * 4, 50.0)

    def _pick_largest(polys: List[List[List[float]]]) -> List[List[List[float]]]:
        if not polys:
            return []
        if len(polys) == 1:
            return polys
        best = max(polys, key=_poly_area_xy)
        return [best]

    # 슬라브(윗면): face loop 우선 — 복잡 brep에서 절단보다 안정적
    if use_top:
        face_polys = _horizontal_faces_at_z(
            brep, z_tgt, z_band, tess_tol, max_z_span, include_inner=True, pick_largest=False,
        )
        if face_polys:
            return face_polys

    # 기둥·벽체(바닥면): face loop 우선
    if not use_top:
        face_polys = _horizontal_faces_at_z(
            brep, z_tgt, z_band, tess_tol, max_z_span, include_inner=False, pick_largest=True,
        )
        if face_polys:
            return face_polys

    # 절단 fallback
    z_multipliers = (
        [0, -0.25, 0.25, -0.5, 0.5, -1.0, 1.0, -2.0, 2.0, -3.0, 3.0]
        if not use_top
        else [0, -0.25, 0.25, -0.5, 0.5, -1.0, -1.5, -2.0]
    )
    for mult in z_multipliers:
        section = _section_polys_at_z(brep, z_tgt + z_band * mult * 0.5, tess_tol, max_z_span)
        if section:
            return section if use_top else _pick_largest(section)

    # face loop 최종 fallback
    face_polys = _horizontal_faces_at_z(
        brep, z_tgt, z_band * 2, tess_tol, max_z_span,
        include_inner=use_top, pick_largest=not use_top,
    )
    return face_polys


def extract_preview_geometry(file_path: str) -> dict:
    """레거시 호환 — meshes 대신 빈 배열 (2D는 member_curves 사용)"""
    return {"edges": [], "meshes": []}


def extract_all_plan_curves(file_path: str, default_strengths: Optional[dict] = None) -> List[dict]:
    """업로드/동기화 시 부재별 plan curve 일괄 추출"""
    results = []
    defaults = default_strengths if default_strengths is not None else {}

    if RHINOINSIDE_AVAILABLE:
        try:
            import Rhino
            from Rhino.Geometry import BrepSolidOrientation
            f = Rhino.FileIO.File3dm.Read(file_path)
            if f is None:
                return results
            for idx, obj in enumerate(f.Objects):
                geo = obj.Geometry
                if geo is None:
                    continue
                gt = geo.GetType().Name
                if gt not in ("Brep", "Extrusion"):
                    continue
                part = normalize_part_type(obj.Attributes.GetUserString("부위") or "")
                brep = geo.ToBrep() if gt == "Extrusion" else geo
                if brep is None:
                    continue
                if brep.IsSolid and brep.SolidOrientation == BrepSolidOrientation.Inward:
                    brep.Flip()
                bbox = brep.GetBoundingBox(False)
                mn, mx = bbox.Min, bbox.Max
                bbox_dict = {
                    "min": {"x": float(mn.X), "y": float(mn.Y), "z": float(mn.Z)},
                    "max": {"x": float(mx.X), "y": float(mx.Y), "z": float(mx.Z)},
                }
                curves = extract_brep_plan_curves(brep, part, bbox_dict)
                results.append({
                    "object_index": idx,
                    "part_type": part,
                    "orientation": get_member_orientation(part),
                    "face": "top" if part == "수평" else "bottom",
                    "curves": curves,
                })
            return results
        except Exception as e:
            logger.warning(f"plan curves bulk extract failed: {e}")

    members = extract_floor_members(file_path, defaults)
    for m in members:
        results.append({
            "object_index": m["object_index"],
            "part_type": m["part_type"],
            "orientation": m["orientation"],
            "face": "top" if m["part_type"] == "수평" else "bottom",
            "curves": _bbox_plan_rectangle(m["bbox"]),
        })
    return results


def _extract_mesh_xy(mesh) -> Optional[dict]:
    try:
        vertices = []
        for i in range(len(mesh.Vertices)):
            v = mesh.Vertices[i]
            vertices.append([v.X, v.Y])
        faces = []
        for i in range(len(mesh.Faces)):
            face = mesh.Faces[i]
            if face[3] == face[2]:
                faces.append([face[0], face[1], face[2]])
            else:
                faces.append([face[0], face[1], face[2], face[3]])
        if not vertices:
            return None
        return {"vertices": vertices, "faces": faces}
    except Exception:
        return None


def _brep_to_mesh_preview(brep) -> Optional[dict]:
    """Brep의 실제 메시를 XY 투영하여 프리뷰 데이터로 반환"""
    try:
        faces_list = brep.Faces
        if faces_list is None or len(faces_list) == 0:
            bbox = brep.GetBoundingBox()
            if bbox:
                return _bbox_to_rect_mesh(bbox)
            return None

        combined_verts = []
        combined_faces = []
        offset = 0

        for i in range(len(faces_list)):
            face = faces_list[i]
            mesh = face.GetMesh(rhino3dm.MeshType.Any)
            if mesh is None:
                continue
            n_verts = len(mesh.Vertices)
            for vi in range(n_verts):
                v = mesh.Vertices[vi]
                combined_verts.append([v.X, v.Y])
            for fi in range(len(mesh.Faces)):
                f = mesh.Faces[fi]
                if f[3] == f[2]:
                    combined_faces.append([f[0] + offset, f[1] + offset, f[2] + offset])
                else:
                    combined_faces.append([f[0] + offset, f[1] + offset, f[2] + offset, f[3] + offset])
            offset += n_verts

        if not combined_verts:
            bbox = brep.GetBoundingBox()
            if bbox:
                return _bbox_to_rect_mesh(bbox)
            return None

        return {"vertices": combined_verts, "faces": combined_faces}
    except Exception:
        try:
            bbox = brep.GetBoundingBox()
            if bbox:
                return _bbox_to_rect_mesh(bbox)
        except Exception:
            pass
        return None


def _extrusion_to_mesh_preview(extrusion) -> Optional[dict]:
    """Extrusion을 Brep 변환 후 실제 메시 프리뷰"""
    try:
        brep = extrusion.ToBrep()
        if brep:
            result = _brep_to_mesh_preview(brep)
            if result:
                return result
    except Exception:
        pass
    try:
        bbox = extrusion.GetBoundingBox()
        if bbox is None:
            return None
        return _bbox_to_rect_mesh(bbox)
    except Exception:
        return None


def _bbox_to_rect_mesh(bbox) -> dict:
    mn = bbox.Min
    mx = bbox.Max
    return {
        "vertices": [[mn.X, mn.Y], [mx.X, mn.Y], [mx.X, mx.Y], [mn.X, mx.Y]],
        "faces": [[0, 1, 2, 3]],
    }


def _bbox_to_edges(bbox) -> list:
    mn = bbox.Min
    mx = bbox.Max
    return [
        [[mn.X, mn.Y], [mx.X, mn.Y]],
        [[mx.X, mn.Y], [mx.X, mx.Y]],
        [[mx.X, mx.Y], [mn.X, mx.Y]],
        [[mn.X, mx.Y], [mn.X, mn.Y]],
    ]


def _mock_preview() -> dict:
    return {
        "edges": [],
        "meshes": [
            {"vertices": [[-45, -25], [45, -25], [45, 25], [-45, 25]], "faces": [[0, 1, 2, 3]]},
        ],
    }


# ---------------------------------------------------------------------------
# 3D Mesh Extraction (시각화용 - rhino3dm)
# ---------------------------------------------------------------------------

def extract_3d_mesh(
    file_path: str,
    default_strengths: Optional[dict] = None,
    strength_zones: Optional[List[dict]] = None,
    member_overrides: Optional[Dict[int, float]] = None,
) -> List[Dict]:
    """3dm 파일을 경량 삼각 메시로 변환 (3D 뷰어 표시용)"""
    if not RHINO3DM_AVAILABLE:
        return _mock_3d_mesh()

    defaults = default_strengths if default_strengths is not None else {}
    zones = strength_zones if strength_zones is not None else []
    overrides = member_overrides if member_overrides is not None else {}

    try:
        model = rhino3dm.File3dm.Read(file_path)
        if model is None:
            return _mock_3d_mesh()

        objects = []
        for idx, obj in enumerate(model.Objects):
            geo = obj.Geometry
            if geo is None:
                continue
            geo_type = type(geo).__name__

            mesh = None
            if geo_type == "Mesh":
                mesh = geo
            elif geo_type == "Brep":
                mesh = _tessellate_brep(geo)
            elif geo_type == "Extrusion":
                mesh = _tessellate_extrusion(geo)

            if mesh is not None:
                mesh_data = _extract_mesh_3d(mesh)
                if mesh_data:
                    ut = _read_object_user_strings(obj.Attributes)
                    part, rhino_val = _member_attrs_from_userstrings(ut)
                    verts = mesh_data["vertices"]
                    cx = sum(v[0] for v in verts) / len(verts)
                    cy = sum(v[1] for v in verts) / len(verts)
                    orientation = get_member_orientation(part)
                    strength = resolve_member_strength(
                        part, rhino_val, {"x": cx, "y": cy}, zones,
                        overrides.get(idx), defaults,
                    )
                    mesh_data["object_index"] = idx
                    mesh_data["part_type"] = part
                    mesh_data["orientation"] = orientation
                    mesh_data["strength"] = strength
                    mesh_data["color"] = mesh_display_color(part, strength, orientation)
                    objects.append(mesh_data)

        if not objects:
            return _mock_3d_mesh()
        return objects
    except Exception as e:
        logger.error(f"Error extracting 3D mesh: {e}")
        return _mock_3d_mesh()


def _tessellate_brep(brep) -> Optional[object]:
    try:
        meshes = brep.Faces
        if meshes is None or len(meshes) == 0:
            return None

        combined_verts = []
        combined_faces = []
        offset = 0

        for i in range(len(meshes)):
            face = meshes[i]
            mesh = face.GetMesh(rhino3dm.MeshType.Any)
            if mesh is None:
                continue
            n_verts = len(mesh.Vertices)
            for vi in range(n_verts):
                v = mesh.Vertices[vi]
                combined_verts.append([v.X, v.Y, v.Z])
            for fi in range(len(mesh.Faces)):
                f = mesh.Faces[fi]
                if f[3] == f[2]:
                    combined_faces.append([f[0] + offset, f[1] + offset, f[2] + offset])
                else:
                    combined_faces.append([f[0] + offset, f[1] + offset, f[2] + offset])
                    combined_faces.append([f[2] + offset, f[3] + offset, f[0] + offset])
            offset += n_verts

        if not combined_verts:
            return None

        class _RawMesh:
            pass
        holder = _RawMesh()
        holder._verts = combined_verts
        holder._faces = combined_faces
        holder._is_raw = True
        return holder
    except Exception:
        bbox = brep.GetBoundingBox()
        if bbox:
            return _bbox_to_box_mesh(bbox)
        return None


def _tessellate_extrusion(extrusion) -> Optional[object]:
    try:
        brep = extrusion.ToBrep()
        if brep:
            result = _tessellate_brep(brep)
            if result:
                return result
    except Exception:
        pass
    try:
        bbox = extrusion.GetBoundingBox()
        if bbox:
            return _bbox_to_box_mesh(bbox)
    except Exception:
        pass
    return None


def _bbox_to_box_mesh(bbox):
    mn = bbox.Min
    mx = bbox.Max
    verts = [
        [mn.X, mn.Y, mn.Z], [mx.X, mn.Y, mn.Z],
        [mx.X, mx.Y, mn.Z], [mn.X, mx.Y, mn.Z],
        [mn.X, mn.Y, mx.Z], [mx.X, mn.Y, mx.Z],
        [mx.X, mx.Y, mx.Z], [mn.X, mx.Y, mx.Z],
    ]
    faces = [
        [0,1,2],[0,2,3],[4,6,5],[4,7,6],
        [0,4,5],[0,5,1],[2,6,7],[2,7,3],
        [0,3,7],[0,7,4],[1,5,6],[1,6,2],
    ]
    class _RawMesh:
        pass
    holder = _RawMesh()
    holder._verts = verts
    holder._faces = faces
    holder._is_raw = True
    return holder


def _extract_mesh_3d(mesh) -> Optional[Dict]:
    try:
        if hasattr(mesh, '_is_raw') and mesh._is_raw:
            verts = mesh._verts
            faces = mesh._faces
            normals = _compute_normals(verts, faces)
            return {"vertices": verts, "faces": faces, "normals": normals}

        vertices = []
        for i in range(len(mesh.Vertices)):
            v = mesh.Vertices[i]
            vertices.append([v.X, v.Y, v.Z])

        faces = []
        for i in range(len(mesh.Faces)):
            f = mesh.Faces[i]
            if f[3] == f[2]:
                faces.append([f[0], f[1], f[2]])
            else:
                faces.append([f[0], f[1], f[2]])
                faces.append([f[2], f[3], f[0]])

        if not vertices:
            return None

        normals = _compute_normals(vertices, faces)
        return {"vertices": vertices, "faces": faces, "normals": normals}
    except Exception:
        return None


def _compute_normals(vertices: List[List[float]], faces: List[List[int]]) -> List[List[float]]:
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for face in faces:
        if len(face) < 3:
            continue
        i0, i1, i2 = face[0], face[1], face[2]
        if i0 >= len(vertices) or i1 >= len(vertices) or i2 >= len(vertices):
            continue
        v0, v1, v2 = vertices[i0], vertices[i1], vertices[i2]
        e1 = [v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2]]
        e2 = [v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2]]
        nx = e1[1]*e2[2] - e1[2]*e2[1]
        ny = e1[2]*e2[0] - e1[0]*e2[2]
        nz = e1[0]*e2[1] - e1[1]*e2[0]
        for idx in face:
            if idx < len(normals):
                normals[idx][0] += nx
                normals[idx][1] += ny
                normals[idx][2] += nz

    for i in range(len(normals)):
        n = normals[i]
        length = math.sqrt(n[0]**2 + n[1]**2 + n[2]**2)
        if length > 0:
            normals[i] = [n[0]/length, n[1]/length, n[2]/length]
        else:
            normals[i] = [0.0, 0.0, 1.0]
    return normals


def _mock_3d_mesh() -> List[Dict]:
    verts = [
        [-45,-25,-10],[45,-25,-10],[45,25,-10],[-45,25,-10],
        [-45,-25,0],[45,-25,0],[45,25,0],[-45,25,0],
    ]
    faces = [
        [0,1,2],[0,2,3],[4,6,5],[4,7,6],
        [0,4,5],[0,5,1],[2,6,7],[2,7,3],
        [0,3,7],[0,7,4],[1,5,6],[1,6,2],
    ]
    normals = _compute_normals(verts, faces)
    return [{"vertices": verts, "faces": faces, "normals": normals}]


# ---------------------------------------------------------------------------
# Polygon Utilities
# ---------------------------------------------------------------------------

def polygon_area(polygon: List[dict]) -> float:
    """2D 닫힌 폴리곤 면적 (Shoelace formula)"""
    n = len(polygon)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += polygon[i]["x"] * polygon[j]["y"]
        area -= polygon[j]["x"] * polygon[i]["y"]
    return abs(area) / 2.0


def polygon_bbox(polygon: List[dict]) -> dict:
    xs = [p["x"] for p in polygon]
    ys = [p["y"] for p in polygon]
    return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}


def compute_remaining_polygon(plan_polygon: List[dict], poured_polygons: List[List[dict]]) -> List[dict]:
    """
    계획 폴리곤에서 이미 타설된 폴리곤들을 차집합하여 잔여 폴리곤(가장 큰 영역)을 반환.
    rhinoinside 미사용 시 계획 폴리곤 그대로 반환.
    """
    if len(plan_polygon) < 3:
        return []
    if not poured_polygons:
        return plan_polygon
    if not RHINOINSIDE_AVAILABLE:
        return plan_polygon

    try:
        import Rhino
        from Rhino.Geometry import Point3d, Polyline, Curve, AreaMassProperties
        from System.Collections.Generic import List as NetList

        def poly_to_curve(poly: List[dict]):
            pts = NetList[Point3d]()
            for p in poly:
                pts.Add(Point3d(float(p["x"]), float(p["y"]), 0.0))
            pts.Add(Point3d(float(poly[0]["x"]), float(poly[0]["y"]), 0.0))
            pl = Polyline(pts)
            return pl.ToPolylineCurve()

        remain_curves = [poly_to_curve(plan_polygon)]
        tol = 0.01

        for poured in poured_polygons:
            if len(poured) < 3:
                continue
            cutter = poly_to_curve(poured)
            next_curves = []
            for base in remain_curves:
                diff = Curve.CreateBooleanDifference(base, cutter, tol)
                if diff and len(diff) > 0:
                    for c in diff:
                        if c and c.IsClosed:
                            next_curves.append(c)
                else:
                    # 차집합 결과가 없으면 base 유지
                    next_curves.append(base)
            remain_curves = next_curves
            if not remain_curves:
                break

        if not remain_curves:
            return []

        # 가장 큰 면적의 곡선 선택
        best_curve = None
        best_area = -1.0
        for c in remain_curves:
            amp = AreaMassProperties.Compute(c)
            area = amp.Area if amp else 0.0
            if area > best_area:
                best_area = area
                best_curve = c

        if best_curve is None:
            return []

        ok, polyline = best_curve.TryGetPolyline()
        if not ok or polyline is None or polyline.Count < 4:
            return plan_polygon

        out = []
        # 마지막 점은 첫 점과 중복되므로 제외
        for i in range(polyline.Count - 1):
            p = polyline[i]
            out.append({"x": float(p.X), "y": float(p.Y)})
        return out if len(out) >= 3 else plan_polygon
    except Exception as e:
        logger.warning(f"잔여 폴리곤 계산 실패, 계획 폴리곤으로 대체: {e}")
        return plan_polygon


# ---------------------------------------------------------------------------
# REAL Boolean Intersection (rhinoinside)
# ---------------------------------------------------------------------------

def calculate_polygon_intersection_volume(
    model_path: str,
    polygon: List[dict],
    z_min: float,
    z_max: float,
) -> dict:
    """
    닫힌 폴리곤을 z_min~z_max로 Extrude하여 Cutter를 만들고,
    원본 3dm 모델과의 Boolean Intersection 체적을 계산.
    """
    if len(polygon) < 3:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["폴리곤 꼭짓점이 3개 미만입니다"]}

    if RHINOINSIDE_AVAILABLE:
        try:
            return _compute_rhinoinside(model_path, polygon, z_min, z_max)
        except Exception as e:
            logger.error(f"rhinoinside 계산 실패: {e}")
            return _compute_mock(model_path, polygon, z_min, z_max)

    return _compute_mock(model_path, polygon, z_min, z_max)


def _compute_rhinoinside(model_path: str, polygon: List[dict], z_min: float, z_max: float) -> dict:
    """rhinoinside를 통한 정확한 Boolean Intersection 체적 계산"""
    import Rhino
    from Rhino.Geometry import (
        Point3d, Vector3d, Polyline, Surface, Brep, VolumeMassProperties
    )
    from System.Collections.Generic import List as NetList

    # 1. 모델 로드
    f = Rhino.FileIO.File3dm.Read(model_path)
    if f is None:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["모델 파일을 읽을 수 없습니다"]}

    # 2. Brep 객체 추출 (BBox 필터링)
    pbbox = polygon_bbox(polygon)
    breps = []
    for obj in f.Objects:
        geo = obj.Geometry
        if geo is None:
            continue
        geo_type = geo.GetType().Name
        if geo_type not in ("Brep", "Extrusion"):
            continue

        bbox = geo.GetBoundingBox(False)
        mn, mx = bbox.Min, bbox.Max
        if mx.X < pbbox["min_x"] or mn.X > pbbox["max_x"]:
            continue
        if mx.Y < pbbox["min_y"] or mn.Y > pbbox["max_y"]:
            continue
        if mx.Z < z_min or mn.Z > z_max:
            continue

        if geo_type == "Extrusion":
            b = geo.ToBrep()
            if b:
                breps.append(b)
        else:
            breps.append(geo)

    if not breps:
        return {"volume": 0.0, "status": "ok", "object_count": 0,
                "warnings": ["교차 범위 내 객체 없음"]}

    # 3. Cutter Brep 생성 (닫힌 폴리곤 Extrude)
    pts = NetList[Point3d]()
    for p in polygon:
        pts.Add(Point3d(p["x"], p["y"], z_min))
    pts.Add(Point3d(polygon[0]["x"], polygon[0]["y"], z_min))

    polyline = Polyline(pts)
    curve = polyline.ToPolylineCurve()

    if not curve.IsClosed:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["폴리라인이 닫히지 않았습니다"]}

    height = z_max - z_min
    direction = Vector3d(0, 0, height)
    surface = Surface.CreateExtrusion(curve, direction)
    if surface is None:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["Cutter surface 생성 실패"]}

    cutter_brep = surface.ToBrep()
    cutter_brep = cutter_brep.CapPlanarHoles(0.001)
    if cutter_brep is None or not cutter_brep.IsSolid:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["Cutter가 닫힌 솔리드가 아닙니다"]}

    # Solid 방향 보정 (Inward 노멀이면 교집합이 반전됨)
    from Rhino.Geometry import BrepSolidOrientation
    orientation = cutter_brep.SolidOrientation
    if orientation == BrepSolidOrientation.Inward:
        cutter_brep.Flip()
        logger.info("Cutter brep flipped (Inward → Outward)")

    # 4. Boolean Intersection
    tolerance = 1.0  # mm 단위 모델에 적합한 tolerance
    total_volume_mm3 = 0.0
    intersected_count = 0
    intersection_meshes = []  # 교집합 결과 메시 (3D 뷰어용)

    # 커터 체적 (검증용)
    cutter_vmp = VolumeMassProperties.Compute(cutter_brep)
    cutter_volume = cutter_vmp.Volume if cutter_vmp else float('inf')
    logger.info(f"Cutter volume: {cutter_volume:.0f} mm³, Breps: {len(breps)}")

    from Rhino.Geometry import Mesh as RhinoMesh, MeshingParameters

    for brep in breps:
        try:
            # 원본 Brep도 SolidOrientation 확인
            if brep.IsSolid and brep.SolidOrientation == BrepSolidOrientation.Inward:
                brep.Flip()
            result = Brep.CreateBooleanIntersection(brep, cutter_brep, tolerance)
            if result and len(result) > 0:
                for rb in result:
                    vmp = VolumeMassProperties.Compute(rb)
                    if vmp:
                        vol = abs(vmp.Volume)
                        # 교집합은 원본보다 작아야 함 (비정상 결과 필터)
                        brep_vmp = VolumeMassProperties.Compute(brep)
                        brep_vol = brep_vmp.Volume if brep_vmp else float('inf')
                        if vol > brep_vol * 0.99:
                            logger.warning(f"교집합 결과가 원본보다 큼 - 건너뜀 (result: {vol:.0f}, original: {brep_vol:.0f})")
                            continue
                        total_volume_mm3 += vol
                        intersected_count += 1
                    # 교집합 Brep을 메시로 변환 (3D 시각화용)
                    try:
                        mp = MeshingParameters(0.5)
                        mp.SimplePlanes = True
                        mesh_array = RhinoMesh.CreateFromBrep(rb, mp)
                        if mesh_array:
                            for m in mesh_array:
                                verts = []
                                for vi in range(m.Vertices.Count):
                                    v = m.Vertices[vi]
                                    verts.append([float(v.X), float(v.Y), float(v.Z)])
                                faces = []
                                for fi in range(m.Faces.Count):
                                    f = m.Faces[fi]
                                    if f.IsTriangle:
                                        faces.append([f.A, f.B, f.C])
                                    else:
                                        faces.append([f.A, f.B, f.C])
                                        faces.append([f.C, f.D, f.A])
                                if verts and faces:
                                    intersection_meshes.append({"vertices": verts, "faces": faces})
                    except Exception as me:
                        logger.warning(f"교집합 메시 변환 실패: {me}")
        except Exception as e:
            logger.warning(f"Boolean intersection failed for one brep: {e}")
            continue

    # 5. 단위 변환 (mm³ → m³)
    max_coord = max(abs(pbbox["max_x"]), abs(pbbox["max_y"]),
                    abs(pbbox["min_x"]), abs(pbbox["min_y"]))
    if max_coord > 1000:
        volume_m3 = total_volume_mm3 / 1e9
    else:
        volume_m3 = total_volume_mm3

    return {
        "volume": round(volume_m3, 4),
        "status": "ok",
        "object_count": intersected_count,
        "warnings": [],
        "intersection_mesh": intersection_meshes,
    }


def _compute_mock(model_path: str, polygon: List[dict], z_min: float, z_max: float) -> dict:
    """Mock 계산: 폴리곤 면적 x 높이 기반 근사 체적"""
    pbbox = polygon_bbox(polygon)
    poly_area = polygon_area(polygon)
    height = abs(z_max - z_min)

    total_volume = 0.0
    obj_count = 0

    if RHINO3DM_AVAILABLE:
        try:
            model = rhino3dm.File3dm.Read(model_path)
            if model:
                for obj in model.Objects:
                    geo = obj.Geometry
                    if geo is None:
                        continue
                    bbox = geo.GetBoundingBox()
                    if bbox is None:
                        continue
                    mn, mx = bbox.Min, bbox.Max

                    ix_min = max(pbbox["min_x"], mn.X)
                    ix_max = min(pbbox["max_x"], mx.X)
                    iy_min = max(pbbox["min_y"], mn.Y)
                    iy_max = min(pbbox["max_y"], mx.Y)
                    iz_min = max(z_min, mn.Z)
                    iz_max = min(z_max, mx.Z)

                    if ix_min < ix_max and iy_min < iy_max and iz_min < iz_max:
                        overlap_area = (ix_max - ix_min) * (iy_max - iy_min)
                        bbox_area = (pbbox["max_x"] - pbbox["min_x"]) * (pbbox["max_y"] - pbbox["min_y"])
                        area_ratio = overlap_area / bbox_area if bbox_area > 0 else 0
                        vol = poly_area * area_ratio * (iz_max - iz_min)
                        total_volume += vol
                        obj_count += 1
        except Exception:
            pass

    if obj_count == 0:
        total_volume = poly_area * height * 0.35
        obj_count = 1

    max_coord = max(abs(pbbox["max_x"]), abs(pbbox["max_y"]),
                    abs(pbbox["min_x"]), abs(pbbox["min_y"]))
    if max_coord > 1000:
        total_volume = total_volume / 1e9

    return {
        "volume": round(total_volume, 4),
        "status": "mock",
        "object_count": obj_count,
        "warnings": ["근사값 (rhinoinside 미사용 - BBox 기반 추정)"],
        "intersection_mesh": [],
    }


# ---------------------------------------------------------------------------
# Member Classification (부위 / VALUE / 수평·수직 / 강도)
# ---------------------------------------------------------------------------

DEFAULT_STRENGTHS = {}
PART_ORIENTATION = {"수평": "horizontal", "기둥": "vertical", "벽체": "vertical"}
VERTICAL_PARTS = {"기둥", "벽체"}


PART_3D_BASE = {
    "수평": "#22d3ee",
    "벽체": "#60a5fa",
    "기둥": "#c084fc",
    "미분류": "#94a3b8",
}

# 부재(행) × 강도(열) — 부재 색상 계열 유지 + 강도별 명확한 차등
MEMBER_COLOR_3D: dict = {
    "수평": {21: "#67e8f9", 24: "#22d3ee", 27: "#06b6d4", 30: "#0891b2", 35: "#fde047", 40: "#facc15", 45: "#eab308"},
    "벽체": {21: "#93c5fd", 24: "#3b82f6", 27: "#2563eb", 30: "#1d4ed8", 35: "#fb923c", 40: "#ea580c", 45: "#dc2626"},
    "기둥": {21: "#d8b4fe", 24: "#a855f7", 27: "#9333ea", 30: "#7e22ce", 35: "#f97316", 40: "#ef4444", 45: "#e11d48"},
    "미분류": {24: "#94a3b8", 35: "#78716c", 40: "#57534e"},
}


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lerp_hex(c0: str, c1: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r0, g0, b0 = _hex_to_rgb(c0)
    r1, g1, b1 = _hex_to_rgb(c1)
    r = int(r0 + (r1 - r0) * t)
    g = int(g0 + (g1 - g0) * t)
    b = int(b0 + (b1 - b0) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def resolve_member_color_3d(part_type: str, strength: Optional[float]) -> str:
    """3D — 부재별 기본 계열 + 강도별 색 (서버·클라이언트 공통 규칙)"""
    part = normalize_part_type(part_type)
    row = MEMBER_COLOR_3D.get(part) or MEMBER_COLOR_3D["미분류"]
    if strength is None:
        return PART_3D_BASE.get(part, PART_3D_BASE["미분류"])
    s = int(round(float(strength)))
    if s in row:
        return row[s]
    keys = sorted(row.keys())
    if s <= keys[0]:
        return row[keys[0]]
    if s >= keys[-1]:
        return row[keys[-1]]
    for i in range(len(keys) - 1):
        lo, hi = keys[i], keys[i + 1]
        if lo <= s <= hi:
            return _lerp_hex(row[lo], row[hi], (s - lo) / (hi - lo))
    return row[keys[-1]]


def mesh_display_color(part_type: str, strength: Optional[float], orientation: str = "horizontal") -> str:
    return resolve_member_color_3d(part_type, strength)


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    """HSL → #rrggbb"""
    s, l = s / 100.0, l / 100.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    ri, gi, bi = int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)
    return f"#{ri:02x}{gi:02x}{bi:02x}"


def strength_color(
    strength: Optional[float], orientation: str = "horizontal", part_type: Optional[str] = None,
) -> str:
    if strength is None:
        return "#64748b"
    if part_type:
        return resolve_member_color_3d(part_type, strength)
    return resolve_member_color_3d("벽체" if orientation == "vertical" else "수평", strength)


def _read_object_user_strings(attributes) -> dict:
    """Rhino/rhino3dm 객체 UserString → dict (튜플·dict·RhinoInside 모두 지원)"""
    ut: dict = {}
    for key in ("부위", "VALUE", "value", "강도"):
        try:
            val = attributes.GetUserString(key)
            if val:
                ut[key] = val
        except Exception:
            pass
    if ut.get("부위"):
        return ut
    try:
        us = attributes.GetUserStrings()
        if isinstance(us, dict):
            ut.update(us)
        elif isinstance(us, (tuple, list)):
            for item in us:
                if isinstance(item, (tuple, list)) and len(item) >= 2:
                    ut[str(item[0])] = str(item[1])
        elif us is not None:
            for j in range(us.Count):
                ut[us.Key(j)] = us.Value(j)
    except Exception:
        pass
    return ut


def _member_attrs_from_userstrings(ut: dict) -> tuple:
    part = normalize_part_type(ut.get("부위", ""))
    rhino_val = ut.get("VALUE") or ut.get("value") or ut.get("강도") or ""
    return part, rhino_val


def normalize_part_type(raw: str) -> str:
    t = (raw or "").strip()
    if t in PART_ORIENTATION:
        return t
    lower = t.lower()
    if "수평" in t or lower in ("h", "horizontal", "slab"):
        return "수평"
    if "기둥" in t or lower in ("col", "column"):
        return "기둥"
    if "벽" in t or lower in ("wall", "w"):
        return "벽체"
    return t or "미분류"


def get_member_orientation(part_type: str) -> str:
    return PART_ORIENTATION.get(normalize_part_type(part_type), "vertical")


def parse_strength_value(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().upper().replace("MPA", "").replace("M", "")
    try:
        return float(s)
    except ValueError:
        digits = "".join(c for c in s if c.isdigit() or c == ".")
        return float(digits) if digits else None


def point_in_polygon(x: float, y: float, polygon: List[dict]) -> bool:
    if len(polygon) < 3:
        return False
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]["x"], polygon[i]["y"]
        xj, yj = polygon[j]["x"], polygon[j]["y"]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def resolve_member_strength(
    part_type: str,
    rhino_value: Optional[str],
    centroid: dict,
    strength_zones: Optional[List[dict]],
    member_override: Optional[float],
    default_strengths: Optional[dict],
) -> Optional[float]:
    if member_override is not None:
        return float(member_override)
    part = normalize_part_type(part_type)
    defaults = default_strengths if default_strengths is not None else {}

    if strength_zones:
        cx, cy = centroid.get("x", 0), centroid.get("y", 0)
        matched_strengths = []
        for zone in strength_zones:
            targets = zone.get("target_parts") or []
            if part not in targets:
                continue
            poly = zone.get("polygon") or []
            if len(poly) >= 3 and point_in_polygon(cx, cy, poly):
                zst = zone.get("strength")
                if zst is not None:
                    matched_strengths.append(float(zst))
        if matched_strengths:
            return max(matched_strengths)

    parsed = parse_strength_value(rhino_value)
    if parsed is not None:
        return parsed

    base = defaults.get(part)
    if base is not None:
        return float(base)
    return None


def _bbox_dict_from_rhino(bbox) -> dict:
    mn, mx = bbox.Min, bbox.Max
    return {
        "min": {"x": float(mn.X), "y": float(mn.Y), "z": float(mn.Z)},
        "max": {"x": float(mx.X), "y": float(mx.Y), "z": float(mx.Z)},
    }


def _volume_to_m3(volume_raw: float, max_coord: float) -> float:
    return volume_raw / 1e9 if max_coord > 1000 else volume_raw


def _strength_label(strength: Optional[float]) -> str:
    if strength is None:
        return "미설정"
    return str(int(round(strength)))


def extract_floor_members(file_path: str, default_strengths: Optional[dict] = None) -> List[dict]:
    """3dm에서 부재(부위 UserString)별 메타데이터·체적 추출"""
    members = []
    defaults = default_strengths if default_strengths is not None else {}

    if RHINOINSIDE_AVAILABLE:
        try:
            import Rhino
            from Rhino.Geometry import VolumeMassProperties, BrepSolidOrientation
            f = Rhino.FileIO.File3dm.Read(file_path)
            if f is None:
                return members
            max_coord = 0.0
            for idx, obj in enumerate(f.Objects):
                geo = obj.Geometry
                if geo is None:
                    continue
                gt = geo.GetType().Name
                if gt not in ("Brep", "Extrusion"):
                    continue
                part = normalize_part_type(obj.Attributes.GetUserString("부위") or "")
                rhino_val = obj.Attributes.GetUserString("VALUE") or obj.Attributes.GetUserString("value") or ""
                brep = geo.ToBrep() if gt == "Extrusion" else geo
                if brep is None:
                    continue
                if brep.IsSolid and brep.SolidOrientation == BrepSolidOrientation.Inward:
                    brep.Flip()
                bbox = brep.GetBoundingBox(False)
                mn, mx = bbox.Min, bbox.Max
                max_coord = max(max_coord, abs(mn.X), abs(mx.X), abs(mn.Y), abs(mx.Y))
                vmp = VolumeMassProperties.Compute(brep)
                vol_raw = abs(vmp.Volume) if vmp else 0.0
                cx = (mn.X + mx.X) / 2
                cy = (mn.Y + mx.Y) / 2
                cz = (mn.Z + mx.Z) / 2
                orientation = get_member_orientation(part)
                strength = resolve_member_strength(part, rhino_val, {"x": cx, "y": cy}, [], None, defaults)
                bbox_dict = _bbox_dict_from_rhino(bbox)
                plan_curves = _simplify_plan_curves(
                    extract_brep_plan_curves(brep, part, bbox_dict), part
                )
                members.append({
                    "object_index": idx,
                    "part_type": part,
                    "orientation": orientation,
                    "rhino_value": rhino_val or None,
                    "strength": strength,
                    "full_volume": round(_volume_to_m3(vol_raw, max_coord), 4),
                    "centroid": {"x": float(cx), "y": float(cy), "z": float(cz)},
                    "bbox": bbox_dict,
                    "plan_curves": plan_curves,
                    "color": strength_color(strength, orientation, part),
                })
            return members
        except Exception as e:
            logger.warning(f"rhinoinside 부재 추출 실패, rhino3dm fallback: {e}")

    if RHINO3DM_AVAILABLE:
        try:
            model = rhino3dm.File3dm.Read(file_path)
            if model is None:
                return members
            max_coord = 0.0
            for idx, obj in enumerate(model.Objects):
                geo = obj.Geometry
                if geo is None:
                    continue
                gt = type(geo).__name__
                if gt not in ("Brep", "Extrusion"):
                    continue
                part = ""
                rhino_val = ""
                try:
                    ut = _read_object_user_strings(obj.Attributes)
                    part, rhino_val = _member_attrs_from_userstrings(ut)
                except Exception:
                    pass
                bbox = geo.GetBoundingBox()
                if bbox is None:
                    continue
                mn, mx = bbox.Min, bbox.Max
                max_coord = max(max_coord, abs(mn.X), abs(mx.X), abs(mn.Y), abs(mx.Y))
                dx, dy, dz = mx.X - mn.X, mx.Y - mn.Y, mx.Z - mn.Z
                vol_raw = dx * dy * dz
                cx, cy, cz = (mn.X + mx.X) / 2, (mn.Y + mx.Y) / 2, (mn.Z + mx.Z) / 2
                orientation = get_member_orientation(part)
                strength = resolve_member_strength(part, rhino_val, {"x": cx, "y": cy}, [], None, defaults)
                bbox_dict = {
                    "min": {"x": float(mn.X), "y": float(mn.Y), "z": float(mn.Z)},
                    "max": {"x": float(mx.X), "y": float(mx.Y), "z": float(mx.Z)},
                }
                members.append({
                    "object_index": idx,
                    "part_type": part,
                    "orientation": orientation,
                    "rhino_value": rhino_val or None,
                    "strength": strength,
                    "full_volume": round(_volume_to_m3(vol_raw, max_coord), 4),
                    "centroid": {"x": float(cx), "y": float(cy), "z": float(cz)},
                    "bbox": bbox_dict,
                    "plan_curves": _bbox_plan_rectangle(bbox_dict),
                    "color": strength_color(strength, orientation, part),
                })
        except Exception as e:
            logger.error(f"부재 추출 실패: {e}")
    return members


def calculate_zone_volume_breakdown(
    model_path: str,
    polygon: List[dict],
    z_min: float,
    z_max: float,
    strength_zones: Optional[List[dict]] = None,
    member_overrides: Optional[Dict[int, float]] = None,
    default_strengths: Optional[dict] = None,
) -> dict:
    """
    타설 구역 폴리곤과 교차하는 부재별 Boolean Intersection 체적을
    수평/수직·강도·부위별로 집계.
    """
    empty = {
        "volume": 0.0, "volume_horizontal": 0.0, "volume_vertical": 0.0,
        "by_strength": {}, "by_part": {}, "members_in_zone": [],
        "status": "ok", "object_count": 0, "warnings": [], "intersection_mesh": [],
    }
    if len(polygon) < 3:
        empty["status"] = "error"
        empty["warnings"] = ["폴리곤 꼭짓점이 3개 미만입니다"]
        return empty

    overrides = member_overrides or {}
    strength_zones = strength_zones or []

    if RHINOINSIDE_AVAILABLE:
        try:
            return _compute_breakdown_rhinoinside(
                model_path, polygon, z_min, z_max,
                strength_zones, overrides, default_strengths,
            )
        except Exception as e:
            logger.error(f"breakdown rhinoinside 실패: {e}")
            return _compute_breakdown_mock(
                model_path, polygon, z_min, z_max,
                strength_zones, overrides, default_strengths,
            )

    return _compute_breakdown_mock(
        model_path, polygon, z_min, z_max,
        strength_zones, overrides, default_strengths,
    )


def _compute_breakdown_rhinoinside(
    model_path: str, polygon: List[dict], z_min: float, z_max: float,
    strength_zones: List[dict], overrides: Dict[int, float],
    default_strengths: Optional[dict],
) -> dict:
    import Rhino
    from Rhino.Geometry import (
        Point3d, Vector3d, Polyline, Surface, Brep, VolumeMassProperties,
        BrepSolidOrientation, Mesh as RhinoMesh, MeshingParameters,
    )
    from System.Collections.Generic import List as NetList

    f = Rhino.FileIO.File3dm.Read(model_path)
    if f is None:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["모델 파일을 읽을 수 없습니다"],
                "volume_horizontal": 0.0, "volume_vertical": 0.0,
                "by_strength": {}, "by_part": {}, "members_in_zone": [],
                "intersection_mesh": []}

    pbbox = polygon_bbox(polygon)
    pts = NetList[Point3d]()
    for p in polygon:
        pts.Add(Point3d(p["x"], p["y"], z_min))
    pts.Add(Point3d(polygon[0]["x"], polygon[0]["y"], z_min))
    polyline = Polyline(pts)
    curve = polyline.ToPolylineCurve()
    if not curve.IsClosed:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["폴리라인이 닫히지 않았습니다"],
                "volume_horizontal": 0.0, "volume_vertical": 0.0,
                "by_strength": {}, "by_part": {}, "members_in_zone": [],
                "intersection_mesh": []}

    height = z_max - z_min
    surface = Surface.CreateExtrusion(curve, Vector3d(0, 0, height))
    cutter_brep = surface.ToBrep().CapPlanarHoles(0.001)
    if cutter_brep is None or not cutter_brep.IsSolid:
        return {"volume": 0.0, "status": "error", "object_count": 0,
                "warnings": ["Cutter 생성 실패"],
                "volume_horizontal": 0.0, "volume_vertical": 0.0,
                "by_strength": {}, "by_part": {}, "members_in_zone": [],
                "intersection_mesh": []}
    if cutter_brep.SolidOrientation == BrepSolidOrientation.Inward:
        cutter_brep.Flip()

    tolerance = 1.0
    max_coord = max(abs(pbbox["max_x"]), abs(pbbox["max_y"]),
                    abs(pbbox["min_x"]), abs(pbbox["min_y"]))

    total_mm3 = 0.0
    vol_h_mm3 = 0.0
    vol_v_mm3 = 0.0
    by_strength: Dict[str, float] = {}
    by_part: Dict[str, float] = {}
    members_in_zone = []
    intersection_meshes = []
    count = 0
    warnings = []

    for idx, obj in enumerate(f.Objects):
        geo = obj.Geometry
        if geo is None:
            continue
        gt = geo.GetType().Name
        if gt not in ("Brep", "Extrusion"):
            continue
        brep = geo.ToBrep() if gt == "Extrusion" else geo
        if brep is None:
            continue

        bbox = brep.GetBoundingBox(False)
        mn, mx = bbox.Min, bbox.Max
        if mx.X < pbbox["min_x"] or mn.X > pbbox["max_x"]:
            continue
        if mx.Y < pbbox["min_y"] or mn.Y > pbbox["max_y"]:
            continue
        if mx.Z < z_min or mn.Z > z_max:
            continue

        part = normalize_part_type(obj.Attributes.GetUserString("부위") or "")
        rhino_val = obj.Attributes.GetUserString("VALUE") or obj.Attributes.GetUserString("value") or ""
        orientation = get_member_orientation(part)
        cx, cy = (mn.X + mx.X) / 2, (mn.Y + mx.Y) / 2
        strength = resolve_member_strength(
            part, rhino_val, {"x": cx, "y": cy},
            strength_zones, overrides.get(idx), default_strengths,
        )
        color = mesh_display_color(part, strength, orientation)

        try:
            if brep.IsSolid and brep.SolidOrientation == BrepSolidOrientation.Inward:
                brep.Flip()
            result = Brep.CreateBooleanIntersection(brep, cutter_brep, tolerance)
            if not result or len(result) == 0:
                continue
            member_mm3 = 0.0
            mesh_added = False
            full_vol_counted = False
            brep_vmp = VolumeMassProperties.Compute(brep)
            brep_vol = abs(brep_vmp.Volume) if brep_vmp else 0.0
            for rb in result:
                vmp = VolumeMassProperties.Compute(rb)
                if not vmp:
                    continue
                vol = abs(vmp.Volume)
                if brep_vol > 0 and vol > brep_vol * 0.99:
                    # Boolean이 전체 brep를 반환 — centroid가 타설구역 안이면 전체 체적 인정
                    if point_in_polygon(cx, cy, polygon) and not full_vol_counted:
                        member_mm3 += brep_vol
                        full_vol_counted = True
                    continue
                member_mm3 += vol
                try:
                    mp = MeshingParameters(0.5)
                    mp.SimplePlanes = True
                    mesh_array = RhinoMesh.CreateFromBrep(rb, mp)
                    if mesh_array:
                        for m in mesh_array:
                            verts = [[float(m.Vertices[vi].X), float(m.Vertices[vi].Y), float(m.Vertices[vi].Z)]
                                     for vi in range(m.Vertices.Count)]
                            faces = []
                            for fi in range(m.Faces.Count):
                                fc = m.Faces[fi]
                                if fc.IsTriangle:
                                    faces.append([fc.A, fc.B, fc.C])
                                else:
                                    faces.append([fc.A, fc.B, fc.C])
                                    faces.append([fc.C, fc.D, fc.A])
                            if verts and faces:
                                intersection_meshes.append({
                                    "vertices": verts, "faces": faces,
                                    "color": mesh_display_color(part, strength, orientation),
                                    "part_type": part,
                                    "orientation": orientation, "strength": strength,
                                })
                                mesh_added = True
                except Exception:
                    pass
            if member_mm3 <= 0:
                continue
            if not mesh_added:
                try:
                    mp = MeshingParameters(0.5)
                    mp.SimplePlanes = True
                    mesh_array = RhinoMesh.CreateFromBrep(brep, mp)
                    if mesh_array:
                        for m in mesh_array:
                            verts = [[float(m.Vertices[vi].X), float(m.Vertices[vi].Y), float(m.Vertices[vi].Z)]
                                     for vi in range(m.Vertices.Count)]
                            faces = []
                            for fi in range(m.Faces.Count):
                                fc = m.Faces[fi]
                                if fc.IsTriangle:
                                    faces.append([fc.A, fc.B, fc.C])
                                else:
                                    faces.append([fc.A, fc.B, fc.C])
                                    faces.append([fc.C, fc.D, fc.A])
                            if verts and faces:
                                intersection_meshes.append({
                                    "vertices": verts, "faces": faces,
                                    "color": mesh_display_color(part, strength, orientation),
                                    "part_type": part,
                                    "orientation": orientation, "strength": strength,
                                })
                except Exception:
                    pass
            vol_m3 = _volume_to_m3(member_mm3, max_coord)
            total_mm3 += member_mm3
            sk = _strength_label(strength)
            by_strength[sk] = by_strength.get(sk, 0.0) + vol_m3
            by_part[part] = by_part.get(part, 0.0) + vol_m3
            if orientation == "horizontal":
                vol_h_mm3 += member_mm3
            else:
                vol_v_mm3 += member_mm3
            members_in_zone.append({
                "object_index": idx, "part_type": part, "orientation": orientation,
                "strength": strength, "volume": round(vol_m3, 4),
                "rhino_value": rhino_val or None,
            })
            count += 1
        except Exception as e:
            warnings.append(f"부재 {idx} 교집합 실패: {e}")
            continue

    total_m3 = round(_volume_to_m3(total_mm3, max_coord), 4)
    return {
        "volume": total_m3,
        "volume_horizontal": round(_volume_to_m3(vol_h_mm3, max_coord), 4),
        "volume_vertical": round(_volume_to_m3(vol_v_mm3, max_coord), 4),
        "by_strength": {k: round(v, 4) for k, v in sorted(by_strength.items())},
        "by_part": {k: round(v, 4) for k, v in by_part.items()},
        "members_in_zone": members_in_zone,
        "status": "ok",
        "object_count": count,
        "warnings": warnings,
        "intersection_mesh": intersection_meshes,
    }


def _compute_breakdown_mock(
    model_path: str, polygon: List[dict], z_min: float, z_max: float,
    strength_zones: List[dict], overrides: Dict[int, float],
    default_strengths: Optional[dict],
) -> dict:
    base = calculate_polygon_intersection_volume(model_path, polygon, z_min, z_max)
    members = extract_floor_members(model_path, default_strengths)
    pbbox = polygon_bbox(polygon)
    poly_area = polygon_area(polygon)
    height = abs(z_max - z_min)
    total = 0.0
    vol_h = 0.0
    vol_v = 0.0
    by_strength: Dict[str, float] = {}
    by_part: Dict[str, float] = {}
    in_zone = []

    for m in members:
        bb = m["bbox"]
        if bb["max"]["x"] < pbbox["min_x"] or bb["min"]["x"] > pbbox["max_x"]:
            continue
        if bb["max"]["y"] < pbbox["min_y"] or bb["min"]["y"] > pbbox["max_y"]:
            continue
        if bb["max"]["z"] < z_min or bb["min"]["z"] > z_max:
            continue
        c = m["centroid"]
        if not point_in_polygon(c["x"], c["y"], polygon):
            continue
        strength = resolve_member_strength(
            m["part_type"], m.get("rhino_value"), c,
            strength_zones, overrides.get(m["object_index"]), default_strengths,
        )
        vol = m["full_volume"]
        total += vol
        sk = _strength_label(strength)
        by_strength[sk] = by_strength.get(sk, 0.0) + vol
        by_part[m["part_type"]] = by_part.get(m["part_type"], 0.0) + vol
        if m["orientation"] == "horizontal":
            vol_h += vol
        else:
            vol_v += vol
        in_zone.append({
            "object_index": m["object_index"], "part_type": m["part_type"],
            "orientation": m["orientation"], "strength": strength,
            "volume": round(vol, 4), "rhino_value": m.get("rhino_value"),
        })

    if not in_zone and base.get("volume", 0) > 0:
        total = base["volume"]
        vol_h = total * 0.6
        vol_v = total * 0.4
        by_strength = {"미설정": round(total, 4)}

    return {
        "volume": round(total, 4) if in_zone else base.get("volume", 0),
        "volume_horizontal": round(vol_h, 4),
        "volume_vertical": round(vol_v, 4),
        "by_strength": {k: round(v, 4) for k, v in by_strength.items()},
        "by_part": {k: round(v, 4) for k, v in by_part.items()},
        "members_in_zone": in_zone,
        "status": base.get("status", "mock"),
        "object_count": len(in_zone) or base.get("object_count", 0),
        "warnings": base.get("warnings", []) + (["근사값 breakdown"] if not RHINOINSIDE_AVAILABLE else []),
        "intersection_mesh": base.get("intersection_mesh", []),
    }


def extract_members_preview(file_path: str, member_overrides: Optional[Dict[int, float]] = None,
                            strength_zones: Optional[List[dict]] = None,
                            default_strengths: Optional[dict] = None) -> List[dict]:
    """2D 평면용 부재 발자국 (bbox XY) + 색상"""
    members = extract_floor_members(file_path, default_strengths)
    overrides = member_overrides or {}
    zones = strength_zones or []
    preview = []
    for m in members:
        strength = resolve_member_strength(
            m["part_type"], m.get("rhino_value"), m["centroid"],
            zones, overrides.get(m["object_index"]), default_strengths,
        )
        bb = m["bbox"]
        preview.append({
            "object_index": m["object_index"],
            "part_type": m["part_type"],
            "orientation": m["orientation"],
            "strength": strength,
            "color": strength_color(strength, m["orientation"], m["part_type"]),
            "footprint": [
                {"x": bb["min"]["x"], "y": bb["min"]["y"]},
                {"x": bb["max"]["x"], "y": bb["min"]["y"]},
                {"x": bb["max"]["x"], "y": bb["max"]["y"]},
                {"x": bb["min"]["x"], "y": bb["max"]["y"]},
            ],
        })
    return preview


# ---------------------------------------------------------------------------
# Adaptive Snap Size
# ---------------------------------------------------------------------------

def compute_snap_size(bbox: Optional[dict]) -> float:
    """모델 BBox 크기 기반 적응형 스냅 크기 계산"""
    if not bbox:
        return 1000.0

    try:
        dx = abs(bbox["max"]["x"] - bbox["min"]["x"])
        dy = abs(bbox["max"]["y"] - bbox["min"]["y"])
        dz = abs(bbox["max"]["z"] - bbox["min"]["z"])
        diagonal = math.sqrt(dx**2 + dy**2 + dz**2)

        snap = diagonal / 200.0
        snap = max(100.0, snap)
        snap = round(snap / 100.0) * 100.0
        return snap
    except Exception:
        return 1000.0
