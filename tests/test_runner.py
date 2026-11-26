"""Tests for ntopcl runner utilities and live nTop Automate integration."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ntop_mcp.config import NTopConfig
from ntop_mcp.graph import NTopNotebook
from ntop_mcp.runner import NTopRunner, build_input_payload, parse_ntop_logs


def test_parse_ntop_logs() -> None:
    stdout = (
        "16:59:16 [I]: Loading notebook...\n"
        "16:59:17 [W]: Block uses deprecated overload\n"
        "16:59:18 [E]: Error generating output template : Output of function not set\n"
    )
    parsed = parse_ntop_logs(stdout, "")
    assert parsed["errors"] == ["Error generating output template : Output of function not set"]
    assert parsed["warnings"] == ["Block uses deprecated overload"]
    assert len(parsed["info"]) == 1


def test_build_input_payload_with_template() -> None:
    template = {
        "title": "Demo",
        "inputs": [
            {"name": "Length", "type": "scalar", "values": 10.0, "units": "mm"},
            {"name": "Count", "type": "integer", "values": 2},
        ],
    }
    payload = build_input_payload({"Length": 25.5}, template=template)
    assert payload["inputs"][0]["values"] == 25.5
    assert payload["inputs"][0]["units"] == "mm"
    assert payload["inputs"][1]["values"] == 2


def test_live_ntopcl_environment_and_edited_output_execution(tmp_path: Path) -> None:
    """Integration test against real ntopcl.exe: set output on a copy, generate templates, and run."""
    cfg = NTopConfig(workdir=tmp_path)
    if not cfg.ntopcl_path or not cfg.ntopcl_path.is_file():
        pytest.skip("ntopcl.exe not installed")

    ver = cfg.get_ntop_version()
    assert ver is not None and "nTop" in ver

    env_sample = os.environ.get("NTOP_TEST_SAMPLE")
    if not env_sample or not Path(env_sample).is_file():
        pytest.skip("Set NTOP_TEST_SAMPLE to a local .ntop path to run live ntopcl execution test")
    sample = Path(env_sample)

    # Copy sample, set output to a real variable block (id 108), and verify ntopcl accepts it!
    nb_copy = tmp_path / "sample_with_output.ntop"
    shutil.copy2(sample, nb_copy)

    nb = NTopNotebook.load(nb_copy)
    # Pick first core.var<real> variable as output
    real_vars = [v for v in nb.list_variables() if v["type"] == "real"]
    assert real_vars, "Expected at least one real variable in sample notebook"
    nb.set_output(real_vars[0]["id"])
    nb.save(nb_copy)

    runner = NTopRunner(cfg)
    tpl = runner.generate_templates(nb_copy, output_dir=tmp_path / "tpl")
    assert tpl["success"] is True
    assert tpl["input_template"] is not None
    assert tpl["output_template"] is not None
    assert tpl["errors"] == []

    # Run headlessly and capture output JSON!
    out_json = tmp_path / "run_out.json"
    run_res = runner.run_notebook(nb_copy, output_json_path=out_json, working_dir=tmp_path)
    assert run_res["success"] is True
    assert run_res["exit_code"] == 0
    assert run_res["output"] is not None
