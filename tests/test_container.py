"""Tests for .ntop binary container round-trip and block graph operations."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from ntop_mcp.container import MagicSection, NTopContainer, NTopFnSection, ObjContainer
from ntop_mcp.graph import NTopNotebook


def _build_synthetic_ntop(tmp_path: Path) -> Path:
    """Create a minimal valid MAGIC%$1 .ntop file for deterministic unit testing."""
    fn_json = {
        "def": {
            "displayname": "Synthetic Test Notebook",
            "description": "Unit test fixture",
            "function": "user_func_test",
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
                    {"instanceId": 103, "modelInputIdx": -1, "meta": {"expression": "", "name": ""}},
                ],
            },
            {
                "id": 101,
                "name": "Length",
                "func": "core.var<real>",
                "type": "real",
                "units": {"length": 1},
                "inputs": [
                    {"instanceId": 102, "modelInputIdx": -1, "meta": {"expression": "100mm", "name": ""}},
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
            {
                "id": 103,
                "name": "Count",
                "func": "core.var<integer>",
                "type": "integer",
                "inputs": [
                    {"instanceId": 104, "modelInputIdx": -1, "meta": {"expression": "", "name": ""}},
                ],
            },
            {
                "id": 104,
                "name": "Int_anon_1",
                "func": "",
                "type": "integer",
                "inputs": [],
            },
            {
                "id": 200,
                "name": "UnusedBlock",
                "func": "core.var<bool>",
                "type": "bool",
                "inputs": [
                    {"instanceId": 201, "modelInputIdx": -1, "meta": {"expression": "", "name": ""}},
                ],
            },
            {
                "id": 201,
                "name": "Bool_anon_1",
                "func": "",
                "type": "bool",
                "inputs": [],
            },
        ],
        "dependencies": [],
        "externalDependencies": [],
        "fninf": None,
        "output": -1,
    }
    leaves = [
        {"id": "102", "type": "real", "value": {"isFinite": True, "units": {"length": 1}, "val": 0.1}},
        {"id": "104", "type": "integer", "value": {"val": 4}},
        {"id": "201", "type": "bool", "value": {"val": True}},
    ]

    turhe = MagicSection("JSON", "turhe", json.dumps({"turver": [5, 49, 2]}, separators=(",", ":")).encode())
    fn_sub = MagicSection("json", "fn", json.dumps(fn_json, separators=(",", ":")).encode())
    idx_sub = MagicSection("json", "index", json.dumps(leaves, separators=(",", ":")).encode())
    leaves_obj = ObjContainer(prefix_bytes=struct.pack("<Q", 8), sub_sections=[idx_sub])
    leaves_sub = MagicSection("obj_container", "leaves", leaves_obj.to_payload())
    main_fn = NTopFnSection(sub_sections=[fn_sub, leaves_sub])
    main_sec = MagicSection("ntopfn", "main", main_fn.to_payload())
    sections_sec = MagicSection(
        "json",
        "sections",
        json.dumps({"decorations": [{"name": "Parameters", "description": "", "collapse": False}], "poles": [0, 2]}).encode(),
    )

    container = NTopContainer(sections=[turhe, main_sec, sections_sec])
    out = tmp_path / "synthetic.ntop"
    container.save(out)
    return out


def test_container_exact_roundtrip(tmp_path: Path) -> None:
    src = _build_synthetic_ntop(tmp_path)
    raw = src.read_bytes()
    parsed = NTopContainer.from_bytes(raw, source_path=src)
    assert parsed.to_bytes() == raw
    assert parsed.section_names() == ["turhe", "main", "sections"]


def test_notebook_inspect_variables_validate_and_edit(tmp_path: Path) -> None:
    src = _build_synthetic_ntop(tmp_path)
    nb = NTopNotebook.load(src)

    meta = nb.inspect()
    assert meta["title"] == "Synthetic Test Notebook"
    assert meta["ntop_version"] == [5, 49, 2]
    assert meta["counts"]["variable_blocks"] == 3
    assert meta["output_block_id"] == -1

    vars_list = nb.list_variables()
    names = {v["name"]: v for v in vars_list}
    assert "Length" in names
    assert names["Length"]["literal_value"]["val"] == pytest.approx(0.1)
    assert names["Length"]["expression"] == "100mm"

    val = nb.validate()
    assert val["valid"] is True
    assert val["automate_ready"] is False
    codes = {w["code"] for w in val["warnings"]}
    assert "OUTPUT_NOT_SET" in codes
    assert "UNREACHABLE_BLOCKS" in codes

    # Edit literal by variable name
    res = nb.set_literal("Length", 0.25, expression="250mm")
    assert res["new_value"]["val"] == pytest.approx(0.25)
    assert res["expression"] == "250mm"

    # Set output block and prune unreachable
    out_res = nb.set_output("Length")
    assert out_res["new_output_block_id"] == 101
    prune_res = nb.prune_to_output()
    assert prune_res["removed_blocks"] == 4  # Count(103,104) + UnusedBlock(200,201)

    edited = tmp_path / "edited.ntop"
    nb.save(edited)

    nb2 = NTopNotebook.load(edited)
    assert nb2.inspect()["output_block_id"] == 101
    assert nb2.validate()["automate_ready"] is True
    v2 = {v["name"]: v for v in nb2.list_variables()}
    assert v2["Length"]["literal_value"]["val"] == pytest.approx(0.25)
    assert v2["Length"]["expression"] == "250mm"


def test_notebook_add_literal_block_variable_and_section(tmp_path: Path) -> None:
    src = _build_synthetic_ntop(tmp_path)
    nb = NTopNotebook.load(src)

    sec_res = nb.add_section("Derived", description="Computed outputs")
    assert sec_res["created"] is True

    lit_two = nb.add_literal("real", 2.0, units={})
    div_blk = nb.add_block(
        func="divide<real,real>",
        block_type="real",
        inputs=[{"instanceId": 101}, {"instanceId": lit_two["block_id"]}],
        name="HalfLengthCalc",
        units={"length": 1},
    )
    var_res = nb.add_variable(
        name="Half Length",
        var_type="real",
        source_block_id=div_blk["block_id"],
        section_name="Derived",
    )
    assert var_res["section"] == "Derived"

    out_path = tmp_path / "added.ntop"
    nb.save(out_path)

    nb2 = NTopNotebook.load(out_path)
    assert nb2.validate()["valid"] is True
    sec_names = [s["name"] for s in nb2.inspect()["notebook_sections"]]
    assert sec_names == ["Parameters", "Derived"]
    vars_by_name = {v["name"]: v for v in nb2.list_variables()}
    assert "Half Length" in vars_by_name
    assert vars_by_name["Half Length"]["section"] == "Derived"


def test_real_ntop_file_byte_exact_roundtrip_if_present() -> None:
    """Verify byte-exact lossless round-trip on a real nTop file when NTOP_TEST_SAMPLE is set."""
    env_sample = os.environ.get("NTOP_TEST_SAMPLE")
    if not env_sample or not Path(env_sample).is_file():
        pytest.skip("Set NTOP_TEST_SAMPLE to a local .ntop path to run live round-trip test")
    sample = Path(env_sample)

    raw = sample.read_bytes()
    container = NTopContainer.from_bytes(raw, source_path=sample)
    assert container.to_bytes() == raw

    nb = NTopNotebook(container)
    info = nb.inspect()
    assert info["ntop_version"] == [5, 49, 2]
    assert info["counts"]["total_blocks"] > 100
    assert len(nb.list_variables()) > 10
