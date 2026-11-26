"""Pure-Python mesh geometry and exported data table analyzer for nTop outputs.

Supports:
- STL (binary and ASCII)
- OBJ (Wavefront)
- 3MF (ZIP/XML 3D Manufacturing Format)
- CSV and JSON exported tables
"""

from __future__ import annotations

import csv
import json
import math
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


Vec3 = Tuple[float, float, float]
Tri = Tuple[Vec3, Vec3, Vec3]


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _quantize_vertex(v: Vec3, tol: float = 1e-6) -> Tuple[int, int, int]:
    return (round(v[0] / tol), round(v[1] / tol), round(v[2] / tol))


def _load_stl_triangles(path: Path) -> Tuple[List[Tri], str]:
    data = path.read_bytes()
    if len(data) >= 84:
        tri_count = struct.unpack_from("<I", data, 80)[0]
        expected_size = 84 + tri_count * 50
        if expected_size == len(data):
            tris: List[Tri] = []
            off = 84
            unpack = struct.Struct("<12fH").unpack_from
            for _ in range(tri_count):
                vals = unpack(data, off)
                v1 = (vals[3], vals[4], vals[5])
                v2 = (vals[6], vals[7], vals[8])
                v3 = (vals[9], vals[10], vals[11])
                tris.append((v1, v2, v3))
                off += 50
            return tris, "stl_binary"

    # ASCII STL
    text = data.decode("utf-8", errors="replace")
    verts: List[Vec3] = []
    tris = []
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("vertex "):
            parts = s.split()
            if len(parts) >= 4:
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(verts) == 3:
                    tris.append((verts[0], verts[1], verts[2]))
                    verts.clear()
    return tris, "stl_ascii"


def _load_obj_triangles(path: Path) -> Tuple[List[Tri], str]:
    vertices: List[Vec3] = []
    tris: List[Tri] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif line.startswith("f "):
            tokens = line.split()[1:]
            idxs: List[int] = []
            for tok in tokens:
                v_str = tok.split("/")[0]
                if not v_str:
                    continue
                vi = int(v_str)
                idxs.append(vi - 1 if vi > 0 else len(vertices) + vi)
            # Fan triangulation
            for i in range(1, len(idxs) - 1):
                tris.append((vertices[idxs[0]], vertices[idxs[i]], vertices[idxs[i + 1]]))
    return tris, "obj"


def _load_3mf_triangles(path: Path) -> Tuple[List[Tri], str]:
    tris: List[Tri] = []
    with zipfile.ZipFile(path, "r") as zf:
        model_files = [n for n in zf.namelist() if n.lower().endswith(".model")]
        if not model_files:
            raise ValueError(f"No .model XML found inside 3MF archive: {path}")
        for mname in model_files:
            root = ET.fromstring(zf.read(mname))
            for elem in root.iter():
                if elem.tag.endswith("mesh"):
                    verts: List[Vec3] = []
                    for child in elem:
                        if child.tag.endswith("vertices"):
                            for v in child:
                                if v.tag.endswith("vertex"):
                                    verts.append((float(v.attrib["x"]), float(v.attrib["y"]), float(v.attrib["z"])))
                        elif child.tag.endswith("triangles"):
                            for t in child:
                                if t.tag.endswith("triangle"):
                                    v1 = verts[int(t.attrib["v1"])]
                                    v2 = verts[int(t.attrib["v2"])]
                                    v3 = verts[int(t.attrib["v3"])]
                                    tris.append((v1, v2, v3))
    return tris, "3mf"


def analyze_mesh_file(path: str | Path) -> Dict[str, Any]:
    """Compute geometric and topological statistics for an STL, OBJ, or 3MF mesh."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Mesh file not found: {p}")

    ext = p.suffix.lower()
    if ext == ".stl":
        tris, fmt = _load_stl_triangles(p)
    elif ext == ".obj":
        tris, fmt = _load_obj_triangles(p)
    elif ext == ".3mf":
        tris, fmt = _load_3mf_triangles(p)
    else:
        raise ValueError(f"Unsupported mesh format {ext!r}. Supported: .stl, .obj, .3mf")

    if not tris:
        return {
            "path": str(p),
            "format": fmt,
            "file_size_bytes": p.stat().st_size,
            "triangle_count": 0,
            "unique_vertex_count": 0,
            "error": "Mesh contains 0 triangles",
        }

    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    area_sum = 0.0
    signed_vol_6 = 0.0
    degenerate_count = 0

    unique_verts = set()
    edge_counts: Counter = Counter()

    for v1, v2, v3 in tris:
        for vx, vy, vz in (v1, v2, v3):
            if vx < min_x:
                min_x = vx
            if vy < min_y:
                min_y = vy
            if vz < min_z:
                min_z = vz
            if vx > max_x:
                max_x = vx
            if vy > max_y:
                max_y = vy
            if vz > max_z:
                max_z = vz

        e1 = _sub(v2, v1)
        e2 = _sub(v3, v1)
        cross = _cross(e1, e2)
        tri_area = 0.5 * _norm(cross)
        if tri_area <= 1e-15:
            degenerate_count += 1
        area_sum += tri_area
        signed_vol_6 += _dot(v1, _cross(v2, v3))

        q1 = _quantize_vertex(v1)
        q2 = _quantize_vertex(v2)
        q3 = _quantize_vertex(v3)
        unique_verts.add(q1)
        unique_verts.add(q2)
        unique_verts.add(q3)

        for a, b in ((q1, q2), (q2, q3), (q3, q1)):
            if a != b:
                edge = (a, b) if a < b else (b, a)
                edge_counts[edge] += 1

    signed_volume = signed_vol_6 / 6.0
    open_edges = sum(1 for c in edge_counts.values() if c == 1)
    non_manifold_edges = sum(1 for c in edge_counts.values() if c > 2)
    is_manifold = non_manifold_edges == 0
    is_watertight = open_edges == 0 and non_manifold_edges == 0

    return {
        "path": str(p),
        "format": fmt,
        "file_size_bytes": p.stat().st_size,
        "triangle_count": len(tris),
        "unique_vertex_count": len(unique_verts),
        "degenerate_triangles": degenerate_count,
        "bounding_box": {
            "min": [round(min_x, 6), round(min_y, 6), round(min_z, 6)],
            "max": [round(max_x, 6), round(max_y, 6), round(max_z, 6)],
            "size": [round(max_x - min_x, 6), round(max_y - min_y, 6), round(max_z - min_z, 6)],
            "center": [
                round(0.5 * (min_x + max_x), 6),
                round(0.5 * (min_y + max_y), 6),
                round(0.5 * (min_z + max_z), 6),
            ],
        },
        "surface_area": round(area_sum, 6),
        "signed_volume": round(signed_volume, 6),
        "volume": round(abs(signed_volume), 6),
        "topology": {
            "total_unique_edges": len(edge_counts),
            "open_boundary_edges": open_edges,
            "non_manifold_edges": non_manifold_edges,
            "is_manifold": is_manifold,
            "is_watertight": is_watertight,
        },
    }


def read_export_table(path: str | Path, max_preview_rows: int = 20) -> Dict[str, Any]:
    """Read and summarize an exported CSV or JSON table file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Data file not found: {p}")

    ext = p.suffix.lower()
    if ext == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            "path": str(p),
            "format": "json",
            "file_size_bytes": p.stat().st_size,
            "content": data,
        }

    if ext in (".csv", ".tsv", ".txt"):
        delimiter = "\t" if ext == ".tsv" else ","
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            columns = reader.fieldnames or []
            rows: List[Dict[str, Any]] = []
            numeric_vals: Dict[str, List[float]] = {c: [] for c in columns}
            for row in reader:
                rows.append(row)
                for c in columns:
                    raw = row.get(c)
                    if raw is not None and raw != "":
                        try:
                            numeric_vals[c].append(float(raw))
                        except ValueError:
                            pass

        stats: Dict[str, Dict[str, float]] = {}
        for c, vals in numeric_vals.items():
            if vals:
                stats[c] = {
                    "count": len(vals),
                    "min": min(vals),
                    "max": max(vals),
                    "mean": round(sum(vals) / len(vals), 6),
                }

        return {
            "path": str(p),
            "format": ext.lstrip("."),
            "file_size_bytes": p.stat().st_size,
            "columns": columns,
            "row_count": len(rows),
            "numeric_stats": stats,
            "preview_rows": rows[:max_preview_rows],
            "truncated": len(rows) > max_preview_rows,
        }

    raise ValueError(f"Unsupported table format {ext!r}. Supported: .csv, .tsv, .json")
