"""Tests for units, batch_edit, visibility, promote_to_input, snapshots, catalog, evaluate_block, and export_block_mesh."""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
from pathlib import Path

import pytest

from ntop_mcp.catalog import build_catalog_from_directory, search_catalog
from ntop_mcp.config import NTopConfig
from ntop_mcp.container import MagicSection, NTopContainer, NTopFnSection, ObjContainer
from ntop_mcp.graph import NTopNotebook
from ntop_mcp.runner import NTopRunner
from ntop_mcp.snapshots import check_unsaved_autosave, create_snapshot, list_snapshots, restore_snapshot
from ntop_mcp.units import parse_quantity


def _make_notebook_with_view(tmp_path: Path) -> Path:
    fn_json = {
        "def": {
            "displayname": "Advanced Test",
            "description": "",
            "function": "user_func_adv",
            "inputs": [],
            "output": {"default_type": "", "type": ""},
            "newSystemOutput": {"type": ""},
        },
        "code": [
            {
                "id": 100,
                "name": "",
                "func": "core.group<any>",
                "type": "any",
                "inputs": [
                    {"instanceId": 101, "modelInputIdx": -1, "meta": {"expression": "", "name": ""}},
                ],
            },
            {
                "id": 101,
                "name": "Wall",
                "func": "core.var<real>",
                "type": "real",
                "units": {"length": 1},
                "inputs": [
                    {"instanceId": 102, "modelInputIdx": -1, "meta": {"expression": "8mm", "name": ""}},
                ],
            },
            {
                "id": 102,
                "name": "Scalar_anon_1",
                "func": "",
                "type": "real",
                "units": {"length": 1},
                "inputs": [],
            },
        ],
        "dependencies": [],
        "externalDependencies": [],
        "fninf": None,
        "output": -1,
    }
    leaves = [
        {"id": "102", "type": "real", "value": {"isFinite": True, "units": {"length": 1}, "val": 0.008}},
    ]
    view_json = [
        {
            "attributes": [
                {"id": 11, "name": "display_mode", "type": 2, "value": 0},
                {"id": 13, "name": "color", "type": 5, "value": [0.7, 0.7, 0.7, 1.0]},
                {"id": 14, "name": "transparency", "type": 1, "value": 0},
                {"id": 15, "name": "opacity", "type": 3, "value": 50},
            ],
            "id": 101,
            "modelId": -1,
            "opacity": 0.5,
            "transparent": False,
            "visible": True,
            "wireframe": False,
        }
    ]

    turhe = MagicSection("JSON", "turhe", json.dumps({"turver": [5, 49, 2]}, separators=(",", ":")).encode())
    fn_sub = MagicSection("json", "fn", json.dumps(fn_json, separators=(",", ":")).encode())
    idx_sub = MagicSection("json", "index", json.dumps(leaves, separators=(",", ":")).encode())
    leaves_obj = ObjContainer(prefix_bytes=struct.pack("<Q", 8), sub_sections=[idx_sub])
    leaves_sub = MagicSection("obj_container", "leaves", leaves_obj.to_payload())
    main_sec = MagicSection("ntopfn", "main", NTopFnSection(sub_sections=[fn_sub, leaves_sub]).to_payload())
    sections_sec = MagicSection(
        "json",
        "sections",
        json.dumps({"decorations": [{"name": "Constants", "description": "", "collapse": False}], "poles": [0, 1]}).encode(),
    )
    view_sec = MagicSection("json", "view", json.dumps(view_json, separators=(",", ":")).encode())

    out = tmp_path / "adv.ntop"
    NTopContainer(sections=[turhe, main_sec, sections_sec, view_sec]).save(out)
    return out


def test_parse_quantity_units() -> None:
    val, units, expr = parse_quantity("10mm")
    assert val == pytest.approx(0.01)
    assert units == {"length": 1}
    assert expr == "10mm"

    val, units, expr = parse_quantity("45 deg")
    assert val == pytest.approx(math.pi / 4.0)
    assert units == {"angle": 1}
    assert expr == "45deg"

    val, units, expr = parse_quantity({"value": 2.5, "unit": "cm"})
    assert val == pytest.approx(0.025)
    assert units == {"length": 1}
    assert expr == "2.5cm"

    val, units, expr = parse_quantity("200MPa")
    assert val == pytest.approx(2e8)
    assert units == {"mass": 1, "length": -1, "time": -2}


def test_batch_edit_visibility_and_promote_to_input(tmp_path: Path) -> None:
    src = _make_notebook_with_view(tmp_path)
    nb = NTopNotebook.load(src)

    res = nb.batch_edit([
        {"op": "set_literal", "target": "Wall", "value": "12mm"},
        {"op": "add_section", "name": "Computed"},
        {"op": "add_literal", "as": "$factor", "leaf_type": "real", "value": 2.0},
        {
            "op": "add_block",
            "as": "$double_wall",
            "func": "multiply<real,real>",
            "block_type": "real",
            "inputs": [101, "$factor"],
            "name": "DoubleWallCalc",
            "units": {"length": 1},
        },
        {
            "op": "add_variable",
            "as": "$double_var",
            "name": "Double Wall",
            "var_type": "real",
            "source_block_id": "$double_wall",
            "section_name": "Computed",
        },
        {
            "op": "set_visibility",
            "targets": ["$double_var"],
            "visible": True,
            "solo": True,
            "color": "#3366ff",
            "transparent": True,
            "opacity": 0.75,
        },
        {"op": "set_output", "target": "$double_var"},
        {"op": "promote_to_input", "target": "Wall", "input_name": "Wall Thickness"},
    ])

    assert res["validation"]["valid"] is True
    assert res["validation"]["automate_ready"] is True
    assert "$double_var" in res["aliases"]

    edited = tmp_path / "adv_edited.ntop"
    nb.save(edited)

    nb2 = NTopNotebook.load(edited)
    meta = nb2.inspect()
    assert len(meta["model_inputs"]) == 1
    assert meta["model_inputs"][0]["name"] == "Wall Thickness"
    assert meta["output_block"]["name"] == "Double Wall"

    view_list = nb2.container.get_section("view").as_json()
    by_id = {v["id"]: v for v in view_list}
    assert by_id[101]["visible"] is False
    dw_id = res["aliases"]["$double_var"]
    assert by_id[dw_id]["visible"] is True
    assert by_id[dw_id]["transparent"] is True
    assert by_id[dw_id]["opacity"] == pytest.approx(0.75)


def test_snapshots_and_undo(tmp_path: Path) -> None:
    src = _make_notebook_with_view(tmp_path)
    orig_bytes = src.read_bytes()

    snap1 = create_snapshot(src, label="first")
    assert snap1 is not None and snap1.is_file()

    # Modify file
    nb = NTopNotebook.load(src)
    nb.set_literal("Wall", "20mm")
    nb.save(src)
    assert src.read_bytes() != orig_bytes

    snaps = list_snapshots(src)
    assert len(snaps) == 1

    undo_res = restore_snapshot(src)
    assert undo_res["from_snapshot"] == snaps[0]["snapshot_name"]
    assert src.read_bytes() == orig_bytes
    assert len(list_snapshots(src)) == 2  # includes before_undo


def test_catalog_build_and_search(tmp_path: Path) -> None:
    src = _make_notebook_with_view(tmp_path)
    cat_path = tmp_path / "catalog.json"
    build_res = build_catalog_from_directory(tmp_path, save_path=cat_path)
    assert build_res["scanned_files"] == 1
    assert build_res["unique_blocks_indexed"] >= 2

    found = search_catalog(query="boolean_union", catalog_path=cat_path)
    assert found["total_matches"] >= 1
    assert any("boolean_union" in b["func"] for b in found["blocks"])


def test_package_custom_block_and_autosave_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_notebook_with_view(tmp_path)
    nb = NTopNotebook.load(src)
    pkg = nb.package_as_custom_block(
        output_target="Wall",
        block_name="My Wall Block",
        input_targets=["Wall"],
        prune=True,
        function_id="user_func_stable_uuid_1234",
    )
    assert pkg["block_name"] == "My Wall Block"
    assert pkg["function_id"] == "user_func_stable_uuid_1234"
    assert pkg["total_inputs"] == 1
    assert pkg["validation"]["valid"] is True

    cb_path = tmp_path / "My_Wall_Block.ntop"
    nb.save(cb_path)
    reloaded = NTopNotebook.load(cb_path)
    assert reloaded.inspect()["title"] == "My Wall Block"
    assert reloaded.inspect()["function_id"] == "user_func_stable_uuid_1234"

    # Simulate a newer Autosave file in APPDATA
    fake_appdata = tmp_path / "AppData"
    autosave_dir = fake_appdata / "nTopology" / "Autosave"
    autosave_dir.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(fake_appdata))

    # Initially no autosave -> False
    assert check_unsaved_autosave(src)["has_newer_autosave"] is False

    # Create a newer autosave file -> True
    newer_auto = autosave_dir / f"{src.stem}.DEADBEEF.ntop"
    newer_auto.write_bytes(src.read_bytes())
    future_ts = src.stat().st_mtime + 10.0
    os.utime(newer_auto, (future_ts, future_ts))
    res = check_unsaved_autosave(src)
    assert res["has_newer_autosave"] is True
    assert res["autosave_path"] == str(newer_auto)


def test_live_evaluate_block_promote_and_export_mesh(tmp_path: Path) -> None:
    """Live ntopcl.exe integration test for evaluate_block, promote_to_input, and export_block_mesh."""
    cfg = NTopConfig(workdir=tmp_path)
    if not cfg.ntopcl_path or not cfg.ntopcl_path.is_file():
        pytest.skip("ntopcl.exe not installed")

    torus_env = os.environ.get("NTOP_TEST_TORUS")
    if not torus_env or not Path(torus_env).is_file():
        pytest.skip("Set NTOP_TEST_TORUS to a local .ntop path to run live mesh export test")
    torus_src = Path(torus_env)

    runner = NTopRunner(cfg)

    # 1. Non-invasive evaluate_block on the torus implicit body
    nb_torus = NTopNotebook.load(torus_src)
    implicit_vars = [v for v in nb_torus.list_variables() if v["type"] in ("implicit", "torus")]
    mesh_vars = [v for v in nb_torus.list_variables() if v["type"] == "mesh"]
    target_var = implicit_vars[0]["name"] if implicit_vars else mesh_vars[0]["name"]

    eval_res = runner.evaluate_block(torus_src, target=target_var, include_properties=True)
    assert eval_res["success"] is True
    assert eval_res["errors"] == []

    # 2. Non-invasive export_block_mesh -> STL + automatic mesh analysis
    stl_out = tmp_path / "torus_exported.stl"
    exp_res = runner.export_block_mesh(
        notebook_path=torus_src,
        target=target_var,
        export_path=stl_out,
        tolerance="1mm",
        unit="mm",
        analyze=True,
    )
    assert exp_res["success"] is True
    assert exp_res["exported"] is True
    assert stl_out.is_file() and stl_out.stat().st_size > 0
    assert exp_res["mesh_analysis"] is not None
    assert exp_res["mesh_analysis"]["triangle_count"] > 100
    assert exp_res["mesh_analysis"]["volume"] > 0

    # 3. Verify promote_to_input works with real ntopcl -t & -j when NTOP_TEST_HEX is set
    hex_env = os.environ.get("NTOP_TEST_HEX")
    hex_src = Path(hex_env) if hex_env else None
    if hex_src and hex_src.is_file():
        hex_copy = tmp_path / "hex_promoted.ntop"
        shutil.copy2(hex_src, hex_copy)
        nb_h = NTopNotebook.load(hex_copy)
        nb_h.promote_to_input("F1 Pipe Thickness", input_name="PipeWall")
        nb_h.set_output("F1 Pipe Thickness")
        nb_h.save(hex_copy)

        tpl = runner.generate_templates(hex_copy)
        assert tpl["success"] is True
        in_names = [i["name"] for i in tpl["input_template"]["inputs"]]
        assert "PipeWall" in in_names

        run_res = runner.run_notebook(hex_copy, inputs={"PipeWall": 12.0}, working_dir=tmp_path)
        assert run_res["success"] is True
        assert run_res["output"] is not None
