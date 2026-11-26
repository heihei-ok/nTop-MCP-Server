"""FastMCP server for nTop (nTopology) automation, notebook editing, and geometry analysis."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP

from .catalog import build_catalog_from_directory, search_catalog
from .config import NTopConfig
from .geometry import analyze_mesh_file, read_export_table as _read_export_table
from .graph import NTopNotebook
from .knowledge import GUIDES, get_guide
from .runner import JobManager, NTopRunner
from .snapshots import (
    close_ntop_processes,
    create_snapshot,
    find_open_ntop_processes,
    list_snapshots,
    restore_snapshot,
)


mcp = FastMCP(
    "ntop-mcp",
    instructions=(
        "MCP server for nTop (nTopology): inspect and edit .ntop notebook block graphs directly, "
        "generate JSON templates, run nTop Automate (ntopcl) headlessly (sync/async), execute "
        "batch parameter sweeps, and analyze exported STL/OBJ/3MF meshes and CSV/JSON tables."
    ),
)

_config = NTopConfig()
_runner = NTopRunner(_config)
_jobs = JobManager(_runner)


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _save_notebook_safely(
    nb: NTopNotebook,
    src: Path,
    output_path: Optional[str],
    overwrite: bool,
    label: str = "edit",
    suffix: str = "_edited",
) -> Dict[str, Any]:
    """Save modified notebook with timestamped snapshot backup and open-GUI conflict detection."""
    if output_path:
        dest = _config.resolve_path(output_path)
    elif overwrite:
        dest = src
    else:
        dest = src.with_name(f"{src.stem}{suffix}{src.suffix}")

    snapshot_path = create_snapshot(dest, label=label) if dest.is_file() else None
    open_procs = find_open_ntop_processes(dest)
    nb.save(dest)

    info: Dict[str, Any] = {
        "saved_to": str(dest),
        "snapshot_backup": str(snapshot_path) if snapshot_path else None,
    }
    if open_procs:
        info["open_gui_pids"] = [p["pid"] for p in open_procs]
        info["open_gui_warning"] = (
            f"nTop GUI currently has {dest.name} open (PIDs: {[p['pid'] for p in open_procs]}). "
            "Close the old window without saving and reopen the file (or call open_in_ntop_gui with reload_if_open=True)."
        )
    return info


# ======================================================================
# 1. Environment & Workspace Tools
# ======================================================================


@mcp.tool()
def check_ntop_environment() -> str:
    """Check nTop installation (ntopcl/ntop GUI), version, working directory, and active jobs."""
    summary = _config.to_summary()
    summary["active_jobs"] = [j for j in _jobs.list_jobs() if j["status"] == "running"]
    return _dump(summary)


@mcp.tool()
def set_workdir(path: str) -> str:
    """Set the default working directory for resolving relative .ntop and export paths.

    Args:
        path: Directory path to set as working directory (created if it does not exist).
    """
    p = Path(path).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    _config.workdir = p
    return _dump({"workdir": str(p), "exists": p.is_dir()})


@mcp.tool()
def list_ntop_files(directory: Optional[str] = None, recursive: bool = False) -> str:
    """List .ntop notebooks and related CAD/mesh/data files in a directory.

    Args:
        directory: Target directory (defaults to current workdir).
        recursive: If True, search subdirectories recursively.
    """
    target = _config.resolve_path(directory) if directory else _config.workdir
    if not target.is_dir():
        return _dump({"error": f"Directory not found: {target}"})

    exts = {".ntop", ".stl", ".3mf", ".obj", ".step", ".stp", ".iges", ".igs", ".csv", ".json"}
    iterator = target.rglob("*") if recursive else target.iterdir()

    grouped: Dict[str, List[Dict[str, Any]]] = {
        "notebooks": [],
        "meshes_and_cad": [],
        "data_and_json": [],
    }
    for p in sorted(iterator):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in exts:
            continue
        info = {"path": str(p), "name": p.name, "size_bytes": p.stat().st_size}
        if ext == ".ntop":
            grouped["notebooks"].append(info)
        elif ext in {".stl", ".3mf", ".obj", ".step", ".stp", ".iges", ".igs"}:
            grouped["meshes_and_cad"].append(info)
        else:
            grouped["data_and_json"].append(info)

    return _dump({"directory": str(target), "recursive": recursive, **grouped})


@mcp.tool()
def open_in_ntop_gui(
    notebook_path: str,
    prevent_duplicate: bool = True,
    reload_if_open: bool = False,
) -> str:
    """Launch an .ntop notebook in the desktop nTop GUI with duplicate-window protection.

    Args:
        notebook_path: Path to the .ntop file.
        prevent_duplicate: If True (default), refuse to open a second window if this file is already open in ntop.exe.
        reload_if_open: If True, close existing ntop.exe window(s) for this file before reopening cleanly.
    """
    if not _config.ntop_gui_path or not _config.ntop_gui_path.is_file():
        return _dump({"error": "nTop GUI executable (ntop.exe) not found. Set NTOP_GUI_PATH."})
    nb = _config.resolve_path(notebook_path)
    if not nb.is_file():
        return _dump({"error": f"Notebook not found: {nb}"})

    open_procs = find_open_ntop_processes(nb)
    closed_pids: List[int] = []
    if open_procs:
        pids = [int(p["pid"]) for p in open_procs if p.get("pid") is not None]
        if reload_if_open:
            closed_pids = close_ntop_processes(pids)
            time.sleep(1.0)
        elif prevent_duplicate:
            return _dump({
                "launched": False,
                "already_open": True,
                "open_pids": pids,
                "notebook": str(nb),
                "message": (
                    f"{nb.name} is already open in nTop GUI (PIDs: {pids}). "
                    "Pass reload_if_open=True to close and reopen cleanly, or prevent_duplicate=False to open a second window."
                ),
            })

    proc = subprocess.Popen([str(_config.ntop_gui_path), str(nb)], cwd=str(nb.parent))
    return _dump({
        "launched": True,
        "pid": proc.pid,
        "closed_previous_pids": closed_pids,
        "executable": str(_config.ntop_gui_path),
        "notebook": str(nb),
    })


# ======================================================================
# 2. Notebook Inspection & Block Graph Analysis Tools
# ======================================================================


@mcp.tool()
def inspect_notebook(notebook_path: str) -> str:
    """Inspect an .ntop file's metadata, nTop version, sections, inputs, output block, and sub-functions.

    Reads the .ntop binary container directly without starting ntopcl (instant).

    Args:
        notebook_path: Path to the .ntop notebook.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        return _dump(nb.inspect())
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def list_notebook_variables(notebook_path: str) -> str:
    """List all promoted variables (core.var<...>) in an .ntop notebook with their IDs, types, units, and values.

    Args:
        notebook_path: Path to the .ntop notebook.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        variables = nb.list_variables()
        return _dump({
            "notebook": str(nb_path),
            "variable_count": len(variables),
            "variables": variables,
        })
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def read_notebook_graph(
    notebook_path: str,
    filter_text: Optional[str] = None,
    section_name: Optional[str] = None,
    variables_only: bool = False,
    include_leaves: bool = False,
    limit: int = 150,
) -> str:
    """Read the block graph (IDs, functions, input wiring, literal values) of an .ntop notebook.

    Args:
        notebook_path: Path to the .ntop notebook.
        filter_text: Optional substring filter matched against block ID, name, function, type, or section.
        section_name: Optional notebook section name filter (case-insensitive substring).
        variables_only: If True, return only promoted variable blocks (core.var<...>).
        include_leaves: If True, also include raw anonymous leaf blocks (func == "").
        limit: Maximum number of blocks to return (default 150).
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        return _dump(
            nb.read_graph(
                filter_text=filter_text,
                section_name=section_name,
                variables_only=variables_only,
                include_leaves=include_leaves,
                limit=limit,
            )
        )
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def validate_notebook_graph(notebook_path: str) -> str:
    """Validate an .ntop notebook's block graph for dangling inputs, duplicate IDs, and Automate readiness.

    Args:
        notebook_path: Path to the .ntop notebook.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        return _dump(nb.validate())
    except Exception as exc:
        return _dump({"error": str(exc)})


# ======================================================================
# 3. Direct Notebook Graph Editing Tools (Lossless Round-Trip)
# ======================================================================


@mcp.tool()
def set_notebook_literal(
    notebook_path: str,
    target: Union[int, str],
    value: Any,
    output_path: Optional[str] = None,
    expression: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Update a literal value (scalar/real, integer, bool, enum, vector/point, text) inside an .ntop file.

    Supports human-friendly unit strings for `real` values such as `"10mm"`, `"2.5cm"`, `"45deg"`,
    or `{"value": 10, "unit": "mm"}` (automatically converted to SI meters/radians + display expression).

    Args:
        notebook_path: Source .ntop file path.
        target: Variable name, variable block ID, or leaf block ID.
        value: New literal value (number, unit string like `"10mm"`, bool, str, or [x, y, z] list).
        output_path: Optional destination .ntop file path.
        expression: Optional explicit display expression to set on a variable block (e.g. "500mm").
        overwrite: If True and output_path is omitted, overwrite notebook_path in place (with timestamped snapshot).
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        change = nb.set_literal(target=target, value=value, expression=expression)
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="set_literal")
        return _dump({**save_info, "change": change})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def set_notebook_output(
    notebook_path: str,
    target: Union[int, str],
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Assign the notebook's Output block so `ntopcl -t` and `ntopcl -o` can export its result.

    Args:
        notebook_path: Source .ntop file path.
        target: Block ID or unique block name to designate as the notebook output.
        output_path: Optional destination .ntop file path.
        overwrite: If True and output_path is omitted, overwrite notebook_path in place (with timestamped snapshot).
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        change = nb.set_output(target)
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="set_output")
        return _dump({**save_info, "change": change})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def rewire_block_input(
    notebook_path: str,
    block_target: Union[int, str],
    input_index: int,
    new_instance_id: Optional[int] = None,
    expression: Optional[str] = None,
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Rewire an input slot of a block to another block ID and/or update its expression string.

    Args:
        notebook_path: Source .ntop file path.
        block_target: Block ID or unique block name to edit.
        input_index: 0-based index into the block's inputs array.
        new_instance_id: New source block ID to connect (or -1 to disconnect).
        expression: Optional expression string for this input slot.
        output_path: Optional destination .ntop file path.
        overwrite: If True and output_path is omitted, overwrite notebook_path in place (with timestamped snapshot).
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        change = nb.rewire_input(
            block_target=block_target,
            input_index=input_index,
            new_instance_id=new_instance_id,
            expression=expression,
        )
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="rewire")
        return _dump({**save_info, "change": change})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def prune_notebook_graph(
    notebook_path: str,
    output_path: str,
    output_target: Optional[Union[int, str]] = None,
) -> str:
    """Prune blocks unreachable from the notebook output block and save to a new .ntop file.

    Args:
        notebook_path: Source .ntop file path.
        output_path: Destination .ntop file path for the pruned notebook.
        output_target: Optional block ID or name to set as output before pruning.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        stats = nb.prune_to_output(output_target=output_target)
        save_info = _save_notebook_safely(nb, nb_path, output_path, False, label="prune")
        return _dump({**save_info, "prune_stats": stats})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def add_notebook_section(
    notebook_path: str,
    section_name: str,
    description: str = "",
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Add a new named Section to an existing .ntop notebook."""
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        res = nb.add_section(name=section_name, description=description)
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="add_section")
        return _dump({**save_info, "result": res})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def add_notebook_literal(
    notebook_path: str,
    leaf_type: str,
    value: Any,
    name: Optional[str] = None,
    units: Optional[Dict[str, int]] = None,
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Add a new literal leaf block (`real`, `integer`, `bool`, `*_enum`, `vector`, `point`, `file_path`) to an .ntop notebook.

    Supports human-friendly unit strings for `real` literals like `"10mm"`, `"45deg"`, or `{"value": 10, "unit": "mm"}`.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        res = nb.add_literal(leaf_type=leaf_type, value=value, name=name, units=units)
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="add_literal")
        return _dump({**save_info, "result": res})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def add_notebook_block(
    notebook_path: str,
    func: str,
    block_type: str,
    inputs: List[Dict[str, Any]],
    name: Optional[str] = None,
    units: Optional[Dict[str, int]] = None,
    section_name: Optional[str] = None,
    add_to_root: bool = False,
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Add a new compute block to an existing .ntop notebook and wire its inputs."""
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        res = nb.add_block(
            func=func,
            block_type=block_type,
            inputs=inputs,
            name=name,
            units=units,
            section_name=section_name,
            add_to_root=add_to_root,
        )
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="add_block")
        return _dump({**save_info, "result": res})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def add_notebook_variable(
    notebook_path: str,
    name: str,
    var_type: str,
    source_block_id: int,
    section_name: Optional[str] = None,
    units: Optional[Dict[str, int]] = None,
    expression: str = "",
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Add a promoted top-level variable (`core.var<var_type>`) in a specified notebook section."""
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        res = nb.add_variable(
            name=name,
            var_type=var_type,
            source_block_id=source_block_id,
            section_name=section_name,
            units=units,
            expression=expression,
        )
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="add_var")
        return _dump({**save_info, "result": res})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def batch_edit_notebook(
    notebook_path: str,
    operations: List[Dict[str, Any]],
    output_path: Optional[str] = None,
    overwrite: bool = False,
    verify_with_ntopcl: bool = False,
    verify_target: Optional[Union[int, str]] = None,
) -> str:
    """Atomically apply multiple graph edits (with `$alias` references) in a single transaction.

    All operations run in memory first; the file is only saved if graph validation passes
    (and if `verify_with_ntopcl=True`, only if `ntopcl` evaluates `verify_target` with zero errors).

    Supported `op` types: `add_section`, `add_literal`, `add_block`, `add_variable`,
    `set_literal`, `rewire_input`, `set_output`, `set_visibility`, `promote_to_input`.

    Args:
        notebook_path: Source .ntop file path.
        operations: Ordered list of operation dicts (use `"as": "$alias"` to bind new block IDs).
        output_path: Optional destination .ntop path.
        overwrite: If True and output_path is omitted, update notebook_path in place (creates snapshot first).
        verify_with_ntopcl: If True, run `ntopcl` on a temp copy before saving; abort save on error.
        verify_target: Block ID, block name, or `"$alias"` to evaluate when `verify_with_ntopcl=True`.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        batch_res = nb.batch_edit(operations)

        if not batch_res["validation"]["valid"]:
            return _dump({
                "saved": False,
                "aborted_reason": "Graph validation failed",
                **batch_res,
            })

        verify_res = None
        if verify_with_ntopcl:
            with tempfile.TemporaryDirectory(prefix="ntop_batch_verify_") as tmp:
                tmp_nb = Path(tmp) / nb_path.name
                nb.save(tmp_nb)
                target_to_eval = verify_target
                if isinstance(target_to_eval, str) and target_to_eval in batch_res["aliases"]:
                    target_to_eval = batch_res["aliases"][target_to_eval]
                elif target_to_eval is None:
                    # Pick last created alias or notebook output
                    if batch_res["aliases"]:
                        target_to_eval = list(batch_res["aliases"].values())[-1]
                    else:
                        target_to_eval = nb.fn.get("output", -1)
                if target_to_eval is None or target_to_eval == -1:
                    return _dump({
                        "saved": False,
                        "aborted_reason": "verify_with_ntopcl=True requires verify_target or an output block",
                        **batch_res,
                    })
                verify_res = _runner.evaluate_block(tmp_nb, target_to_eval, include_properties=True)
                if not verify_res["success"] or verify_res["errors"]:
                    return _dump({
                        "saved": False,
                        "aborted_reason": "ntopcl verification failed; original notebook untouched",
                        "verification": verify_res,
                        **batch_res,
                    })

        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="batch_edit")
        return _dump({
            "saved": True,
            **save_info,
            "verification": verify_res,
            **batch_res,
        })
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def set_block_visibility(
    notebook_path: str,
    targets: List[Union[int, str]],
    visible: bool = True,
    solo: bool = False,
    color: Optional[Any] = None,
    transparent: Optional[bool] = None,
    opacity: Optional[float] = None,
    wireframe: Optional[bool] = None,
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Control viewport visibility, solo mode, color (`#RRGGBB` or `[r,g,b,a]`), transparency, and opacity.

    Args:
        notebook_path: Source .ntop file path.
        targets: List of block IDs or block names to style.
        visible: Show (`True`) or hide (`False`) the target blocks.
        solo: If True, hide all other blocks in the viewport so only `targets` are shown.
        color: Optional hex color (`"#3388ff"`) or `[r, g, b, a]` list.
        transparent: Optional boolean to toggle transparency.
        opacity: Optional opacity from `0.0` to `1.0`.
        wireframe: Optional wireframe toggle.
        output_path: Optional destination .ntop path.
        overwrite: If True and output_path is omitted, update in place (creates snapshot first).
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        res = nb.set_visibility(
            targets=targets,
            visible=visible,
            solo=solo,
            color=color,
            transparent=transparent,
            opacity=opacity,
            wireframe=wireframe,
        )
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="visibility")
        return _dump({**save_info, "result": res})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def promote_to_input(
    notebook_path: str,
    target: Union[int, str],
    input_name: Optional[str] = None,
    description: str = "",
    output_path: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Promote an existing notebook variable (`core.var<...>`) into a Notebook Input (`def.inputs`).

    Enables passing parameter values via `ntopcl -j` and `run_parameter_sweep` without breaking downstream wiring.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        nb = NTopNotebook.load(nb_path)
        res = nb.promote_to_input(target=target, input_name=input_name, description=description)
        save_info = _save_notebook_safely(nb, nb_path, output_path, overwrite, label="promote_input")
        return _dump({**save_info, "result": res})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def list_notebook_snapshots(notebook_path: str) -> str:
    """List timestamped backup snapshots saved in `.ntop_history/` for a notebook."""
    try:
        nb_path = _config.resolve_path(notebook_path)
        snaps = list_snapshots(nb_path)
        return _dump({"notebook": str(nb_path), "snapshot_count": len(snaps), "snapshots": snaps})
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def undo_notebook_edit(notebook_path: str, snapshot_name: Optional[str] = None) -> str:
    """Restore an .ntop notebook from its latest (or named) snapshot in `.ntop_history/`.

    Saves the current state as a `before_undo` snapshot first so undo is itself reversible.
    """
    try:
        nb_path = _config.resolve_path(notebook_path)
        res = restore_snapshot(nb_path, snapshot_name=snapshot_name)
        open_procs = find_open_ntop_processes(nb_path)
        if open_procs:
            res["open_gui_pids"] = [p["pid"] for p in open_procs]
            res["open_gui_warning"] = "Reopen the file in nTop GUI to see the restored state."
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


# ======================================================================
# 4. nTop Automate (`ntopcl`) Execution & Parameter Sweep Tools
# ======================================================================


@mcp.tool()
def generate_templates(
    notebook_path: str,
    output_dir: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Run `ntopcl -t` to generate and return official input_template.json and output_template.json.

    Args:
        notebook_path: Path to the .ntop notebook.
        output_dir: Optional directory to persist the generated JSON templates (uses a temp dir if omitted).
        timeout_seconds: Optional timeout override in seconds.
    """
    try:
        res = _runner.generate_templates(
            notebook_path=notebook_path,
            output_dir=output_dir,
            timeout=timeout_seconds,
        )
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def run_notebook(
    notebook_path: str,
    inputs: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    input_json_path: Optional[str] = None,
    output_json_path: Optional[str] = None,
    working_dir: Optional[str] = None,
    include_properties: bool = False,
    save_notebook: bool = False,
    trust_notebook: bool = False,
    max_threads: Optional[int] = None,
    verbosity: int = 2,
    custom_block_libraries: Optional[Dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Execute an .ntop notebook headlessly via nTop Automate (`ntopcl`).

    Args:
        notebook_path: Path to the .ntop notebook.
        inputs: Either full nTop JSON schema (`{"inputs": [...]}`) or a simple dict (`{"Length": 10.0}`) auto-mapped onto the template.
        input_json_path: Optional path to an existing nTop input JSON file.
        output_json_path: Optional path where nTop should write its output JSON.
        working_dir: Optional working directory for execution and relative exports.
        include_properties: Pass `-p` to include top-level properties in output JSON.
        save_notebook: Pass `-s` to overwrite the .ntop file with computed results.
        trust_notebook: Pass `--trustnotebook` to permit `Run Command` blocks.
        max_threads: Limit CPU threads via `-m`.
        verbosity: Log verbosity 0 (errors), 1 (warnings+errors), 2 (full progress).
        custom_block_libraries: Mapping of `{library_name: directory_path}` for `--custom-block-library`.
        timeout_seconds: Maximum wait time in seconds.
    """
    try:
        res = _runner.run_notebook(
            notebook_path=notebook_path,
            inputs=inputs,
            input_json_path=input_json_path,
            output_json_path=output_json_path,
            working_dir=working_dir,
            include_properties=include_properties,
            save_notebook=save_notebook,
            trust_notebook=trust_notebook,
            max_threads=max_threads,
            verbosity=verbosity,
            custom_block_libraries=custom_block_libraries,
            timeout_seconds=timeout_seconds,
        )
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def run_notebook_async(
    notebook_path: str,
    inputs: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    input_json_path: Optional[str] = None,
    output_json_path: Optional[str] = None,
    working_dir: Optional[str] = None,
    include_properties: bool = False,
    save_notebook: bool = False,
    trust_notebook: bool = False,
    max_threads: Optional[int] = None,
    verbosity: int = 2,
    custom_block_libraries: Optional[Dict[str, str]] = None,
) -> str:
    """Submit a long-running nTop Automate (`ntopcl`) execution in the background and return a job_id.

    Monitor progress with `get_job_status(job_id)` or cancel with `cancel_job(job_id)`.
    """
    try:
        job = _jobs.submit(
            notebook_path=notebook_path,
            inputs=inputs,
            input_json_path=input_json_path,
            output_json_path=output_json_path,
            working_dir=working_dir,
            include_properties=include_properties,
            save_notebook=save_notebook,
            trust_notebook=trust_notebook,
            max_threads=max_threads,
            verbosity=verbosity,
            custom_block_libraries=custom_block_libraries,
        )
        return _dump(job.to_summary(tail_lines=10))
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def get_job_status(job_id: str, tail_lines: int = 40) -> str:
    """Get status, elapsed time, parsed logs, and output of a background ntopcl job.

    Args:
        job_id: The job identifier returned by `run_notebook_async`.
        tail_lines: Number of recent stdout/stderr lines to return.
    """
    try:
        return _dump(_jobs.get(job_id).to_summary(tail_lines=tail_lines))
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def list_jobs() -> str:
    """List all submitted background ntopcl jobs and their current states."""
    return _dump({"jobs": _jobs.list_jobs()})


@mcp.tool()
def cancel_job(job_id: str) -> str:
    """Cancel/terminate a currently running background ntopcl job."""
    try:
        return _dump(_jobs.cancel(job_id))
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def run_parameter_sweep(
    notebook_path: str,
    parameter_sets: Optional[List[Dict[str, Any]]] = None,
    parameter_grid: Optional[Dict[str, List[Any]]] = None,
    output_dir: Optional[str] = None,
    max_workers: int = 1,
    max_threads_per_run: Optional[int] = None,
    include_properties: bool = False,
    trust_notebook: bool = False,
    timeout_per_run: Optional[int] = None,
    export_csv: bool = True,
) -> str:
    """Run a batch DOE / parameter sweep across multiple input sets using `ntopcl`.

    Provide EITHER `parameter_sets` (list of explicit input dicts) OR `parameter_grid`
    (dict of `{param_name: [v1, v2, ...]}` expanded as a Cartesian product).

    Args:
        notebook_path: Path to the .ntop notebook.
        parameter_sets: Explicit list of parameter dictionaries, one per run.
        parameter_grid: Cartesian grid dictionary of parameter value lists.
        output_dir: Directory to store `run_000/`, `run_001/`, ... and `sweep_summary.csv`.
        max_workers: Parallel `ntopcl` worker processes (default 1).
        max_threads_per_run: Pass `-m` to each `ntopcl` invocation.
        include_properties: Pass `-p` to include top-level properties in each output.json.
        trust_notebook: Pass `--trustnotebook` if needed.
        timeout_per_run: Timeout per individual run in seconds.
        export_csv: Write a consolidated `sweep_summary.csv` table.
    """
    try:
        res = _runner.run_parameter_sweep(
            notebook_path=notebook_path,
            parameter_sets=parameter_sets,
            parameter_grid=parameter_grid,
            output_dir=output_dir,
            max_workers=max_workers,
            max_threads_per_run=max_threads_per_run,
            include_properties=include_properties,
            trust_notebook=trust_notebook,
            timeout_per_run=timeout_per_run,
            export_csv=export_csv,
        )
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


# ======================================================================
# 5. Non-Invasive Evaluation, Export, Geometry & Block Catalog Tools
# ======================================================================


@mcp.tool()
def evaluate_block(
    notebook_path: str,
    target: Union[int, str],
    inputs: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    include_properties: bool = True,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Non-invasively evaluate any block (scalar, implicit body, plane, etc.) via `ntopcl`.

    Runs on an isolated temporary copy so your original `.ntop` file is never modified.
    Returns evaluated scalar values, bounding box, properties, and execution timing.
    """
    try:
        res = _runner.evaluate_block(
            notebook_path=notebook_path,
            target=target,
            inputs=inputs,
            include_properties=include_properties,
            timeout_seconds=timeout_seconds,
        )
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def export_block_mesh(
    notebook_path: str,
    target: Union[int, str],
    export_path: str,
    tolerance: Any = "1mm",
    unit: str = "mm",
    sharpen: bool = False,
    simplify: bool = False,
    inputs: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
    analyze: bool = True,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Non-invasively mesh an implicit/mesh block and export to `.stl`, `.3mf`, or `.obj` via `ntopcl`.

    Never modifies your `.ntop` file. Automatically runs `analyze_mesh` on the exported mesh
    so you immediately get volume, surface area, bounding box, and watertightness — ready for COMSOL/Abaqus.

    Args:
        notebook_path: Path to the source `.ntop` file.
        target: Block ID or unique name (e.g. `"Final Flanges and Pipes"`).
        export_path: Destination mesh path (`.stl`, `.3mf`, `.obj`, `.ply`).
        tolerance: Meshing tolerance (supports `"1mm"`, `"0.5mm"`, or SI float meters).
        unit: Export length unit (`"mm"`, `"cm"`, `"m"`, `"in"`).
        sharpen: Enable sharp-edge preservation in `Mesh from Implicit Body`.
        simplify: Enable mesh simplification.
        inputs: Optional nTop Automate inputs dict/list.
        analyze: If True (default), analyze exported mesh geometry & watertightness.
        timeout_seconds: Optional timeout override in seconds.
    """
    try:
        res = _runner.export_block_mesh(
            notebook_path=notebook_path,
            target=target,
            export_path=export_path,
            tolerance=tolerance,
            unit=unit,
            sharpen=sharpen,
            simplify=simplify,
            inputs=inputs,
            analyze=analyze,
            timeout_seconds=timeout_seconds,
        )
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def build_block_catalog(
    directory: str,
    save_path: Optional[str] = None,
    include_custom_blocks: bool = False,
) -> str:
    """Scan a directory of `.ntop` files recursively to index verified local nTop block signatures.

    Args:
        directory: Folder containing `.ntop` notebooks (e.g. `C:\\path\\to\\ntop_examples`).
        save_path: Optional output JSON catalog path (defaults to `block_catalog.json` in package root).
        include_custom_blocks: If True, also include `user_func_*` custom blocks.
    """
    try:
        res = build_catalog_from_directory(
            directory=_config.resolve_path(directory),
            save_path=_config.resolve_path(save_path) if save_path else None,
            include_custom_blocks=include_custom_blocks,
        )
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def search_blocks(
    query: str = "",
    category: Optional[str] = None,
    catalog_path: Optional[str] = None,
    limit: int = 25,
) -> str:
    """Search verified nTop block signatures (function names, output types, input slots) across built-in & local catalog.

    Args:
        query: Keyword substring (e.g. `"shell"`, `"boolean"`, `"mesh"`, `"export"`, `"fe_"`).
        category: Optional category filter (`modeling`, `geometry`, `meshing_and_fe`, `export_import`, `math_and_fields`, `core`, `lattice`).
        catalog_path: Optional custom catalog JSON path.
        limit: Maximum number of matches to return.
    """
    try:
        res = search_catalog(
            query=query,
            category=category,
            catalog_path=_config.resolve_path(catalog_path) if catalog_path else None,
            limit=limit,
        )
        return _dump(res)
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def analyze_mesh(mesh_path: str) -> str:
    """Analyze an exported STL (binary/ASCII), OBJ, or 3MF mesh file.

    Returns triangle/vertex counts, bounding box, surface area, signed/absolute volume,
    and topology checks (open boundary edges, non-manifold edges, watertightness).

    Args:
        mesh_path: Path to a `.stl`, `.obj`, or `.3mf` file.
    """
    try:
        p = _config.resolve_path(mesh_path)
        return _dump(analyze_mesh_file(p))
    except Exception as exc:
        return _dump({"error": str(exc)})


@mcp.tool()
def read_export_table(file_path: str, max_preview_rows: int = 20) -> str:
    """Read and summarize an exported CSV, TSV, or JSON table produced by nTop.

    Args:
        file_path: Path to `.csv`, `.tsv`, or `.json` file.
        max_preview_rows: Maximum preview rows to return for CSV/TSV files.
    """
    try:
        p = _config.resolve_path(file_path)
        return _dump(_read_export_table(p, max_preview_rows=max_preview_rows))
    except Exception as exc:
        return _dump({"error": str(exc)})


# ======================================================================
# 6. Knowledge & MCP Resources
# ======================================================================


@mcp.tool()
def get_ntop_guide(topic: str = "list") -> str:
    """Retrieve built-in nTop Automate and MCP workflow documentation.

    Args:
        topic: One of `list`, `ntopcl_cli`, `json_inputs`, `json_outputs`,
               `notebook_preparation`, `parameter_sweep`, `troubleshooting`.
    """
    return _dump(get_guide(topic))


@mcp.resource("ntop://environment")
def resource_environment() -> str:
    """Current nTop environment status as a JSON resource."""
    return _dump(_config.to_summary())


@mcp.resource("ntop://guides/{topic}")
def resource_guide(topic: str) -> str:
    """Built-in nTop documentation guide."""
    return GUIDES.get(topic, _dump(get_guide(topic)))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
