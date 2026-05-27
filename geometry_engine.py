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
# Preview Geometry Extraction (2D용 - rhino3dm)
# ---------------------------------------------------------------------------

def extract_preview_geometry(file_path: str) -> dict:
    """3dm 파일에서 XY 평면 투영 엣지 및 메시 데이터 추출"""
    if not RHINO3DM_AVAILABLE:
        return _mock_preview()

    try:
        model = rhino3dm.File3dm.Read(file_path)
        if model is None:
            return _mock_preview()

        edges = []
        meshes = []

        for obj in model.Objects:
            geo = obj.Geometry
            if geo is None:
                continue

            geo_type = type(geo).__name__

            if geo_type == "Mesh":
                mesh_data = _extract_mesh_xy(geo)
                if mesh_data:
                    meshes.append(mesh_data)
            elif geo_type == "Brep":
                mesh = _brep_to_mesh_preview(geo)
                if mesh:
                    meshes.append(mesh)
            elif geo_type == "Extrusion":
                mesh = _extrusion_to_mesh_preview(geo)
                if mesh:
                    meshes.append(mesh)
            else:
                bbox = geo.GetBoundingBox()
                if bbox:
                    edges.append(_bbox_to_edges(bbox))

        return {"edges": edges, "meshes": meshes}
    except Exception as e:
        logger.error(f"Error extracting preview: {e}")
        return _mock_preview()


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

def extract_3d_mesh(file_path: str) -> List[Dict]:
    """3dm 파일을 경량 삼각 메시로 변환 (3D 뷰어 표시용)"""
    if not RHINO3DM_AVAILABLE:
        return _mock_3d_mesh()

    try:
        model = rhino3dm.File3dm.Read(file_path)
        if model is None:
            return _mock_3d_mesh()

        objects = []
        for obj in model.Objects:
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
