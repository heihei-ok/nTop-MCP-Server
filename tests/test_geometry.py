"""Tests for pure-Python STL/OBJ/3MF mesh analysis and CSV/JSON export reading."""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from ntop_mcp.geometry import analyze_mesh_file, read_export_table


# Outward-oriented unit cube [0,1]^3 (12 triangles, volume=1.0, surface_area=6.0)
CUBE_VERTS = [
    (0.0, 0.0, 0.0),  # 0
    (1.0, 0.0, 0.0),  # 1
    (1.0, 1.0, 0.0),  # 2
    (0.0, 1.0, 0.0),  # 3
    (0.0, 0.0, 1.0),  # 4
    (1.0, 0.0, 1.0),  # 5
    (1.0, 1.0, 1.0),  # 6
    (0.0, 1.0, 1.0),  # 7
]
CUBE_FACES = [
    (0, 2, 1), (0, 3, 2),  # bottom z=0
    (4, 5, 6), (4, 6, 7),  # top z=1
    (0, 1, 5), (0, 5, 4),  # front y=0
    (2, 3, 7), (2, 7, 6),  # back y=1
    (0, 4, 7), (0, 7, 3),  # left x=0
    (1, 2, 6), (1, 6, 5),  # right x=1
]


def _write_binary_stl(path: Path, faces=CUBE_FACES) -> None:
    buf = bytearray(b"unit cube".ljust(80, b"\x00"))
    buf += struct.pack("<I", len(faces))
    for i, j, k in faces:
        v1, v2, v3 = CUBE_VERTS[i], CUBE_VERTS[j], CUBE_VERTS[k]
        buf += struct.pack("<12fH", 0.0, 0.0, 0.0, *v1, *v2, *v3, 0)
    path.write_bytes(bytes(buf))


def _write_ascii_stl(path: Path) -> None:
    lines = ["solid cube"]
    for i, j, k in CUBE_FACES:
        v1, v2, v3 = CUBE_VERTS[i], CUBE_VERTS[j], CUBE_VERTS[k]
        lines.append("  facet normal 0 0 0")
        lines.append("    outer loop")
        lines.append(f"      vertex {v1[0]} {v1[1]} {v1[2]}")
        lines.append(f"      vertex {v2[0]} {v2[1]} {v2[2]}")
        lines.append(f"      vertex {v3[0]} {v3[1]} {v3[2]}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid cube")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_obj(path: Path) -> None:
    lines = [f"v {x} {y} {z}" for x, y, z in CUBE_VERTS]
    lines += [f"f {i+1} {j+1} {k+1}" for i, j, k in CUBE_FACES]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_3mf(path: Path) -> None:
    v_xml = "\n".join(f'<vertex x="{x}" y="{y}" z="{z}" />' for x, y, z in CUBE_VERTS)
    t_xml = "\n".join(f'<triangle v1="{i}" v2="{j}" v3="{k}" />' for i, j, k in CUBE_FACES)
    model_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>{v_xml}</vertices>
        <triangles>{t_xml}</triangles>
      </mesh>
    </object>
  </resources>
</model>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", model_xml)


def test_analyze_binary_ascii_obj_3mf_unit_cube(tmp_path: Path) -> None:
    p_bin = tmp_path / "cube_bin.stl"
    p_asc = tmp_path / "cube_asc.stl"
    p_obj = tmp_path / "cube.obj"
    p_3mf = tmp_path / "cube.3mf"

    _write_binary_stl(p_bin)
    _write_ascii_stl(p_asc)
    _write_obj(p_obj)
    _write_3mf(p_3mf)

    for p, expected_fmt in [
        (p_bin, "stl_binary"),
        (p_asc, "stl_ascii"),
        (p_obj, "obj"),
        (p_3mf, "3mf"),
    ]:
        res = analyze_mesh_file(p)
        assert res["format"] == expected_fmt
        assert res["triangle_count"] == 12
        assert res["unique_vertex_count"] == 8
        assert res["surface_area"] == pytest.approx(6.0)
        assert res["volume"] == pytest.approx(1.0)
        assert res["bounding_box"]["size"] == [1.0, 1.0, 1.0]
        assert res["topology"]["is_watertight"] is True
        assert res["topology"]["open_boundary_edges"] == 0
        assert res["topology"]["non_manifold_edges"] == 0


def test_analyze_open_mesh_detects_boundary_edges(tmp_path: Path) -> None:
    p_open = tmp_path / "open_box.stl"
    _write_binary_stl(p_open, faces=CUBE_FACES[2:])  # remove bottom face
    res = analyze_mesh_file(p_open)
    assert res["triangle_count"] == 10
    assert res["topology"]["is_watertight"] is False
    assert res["topology"]["open_boundary_edges"] == 4


def test_read_export_table_csv_and_json(tmp_path: Path) -> None:
    csv_path = tmp_path / "results.csv"
    csv_path.write_text("case,mass_g,area_mm2\nA,10.5,100.0\nB,20.5,200.0\n", encoding="utf-8")
    res_csv = read_export_table(csv_path)
    assert res_csv["row_count"] == 2
    assert res_csv["numeric_stats"]["mass_g"]["mean"] == pytest.approx(15.5)

    json_path = tmp_path / "out.json"
    json_path.write_text(json.dumps({"outputs": [{"name": "Mass", "value": 42.0}]}), encoding="utf-8")
    res_json = read_export_table(json_path)
    assert res_json["format"] == "json"
    assert res_json["content"]["outputs"][0]["value"] == 42.0
