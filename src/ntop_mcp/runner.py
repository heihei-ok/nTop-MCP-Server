"""nTop Automate (`ntopcl`) execution engine, async job manager, and parameter sweep runner."""

from __future__ import annotations

import csv
import itertools
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import NTopConfig
from .geometry import analyze_mesh_file
from .graph import NTopNotebook


_LOG_LINE_RE = re.compile(r"^\s*(?:\d{2}:\d{2}:\d{2}\s+)?\[([EWID])\]:\s*(.*)$")


def parse_ntop_logs(stdout: str, stderr: str) -> Dict[str, List[str]]:
    """Extract structured errors and warnings from ntopcl console output."""
    errors: List[str] = []
    warnings: List[str] = []
    info: List[str] = []

    combined = "\n".join(part for part in (stdout, stderr) if part)
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _LOG_LINE_RE.match(line)
        if m:
            level, msg = m.group(1), m.group(2).strip()
            if level == "E":
                errors.append(msg)
            elif level == "W":
                warnings.append(msg)
            else:
                info.append(msg)
        elif "error" in line.lower() and not line.lower().startswith("0 errors"):
            errors.append(line)
        elif "warning" in line.lower():
            warnings.append(line)

    return {"errors": errors, "warnings": warnings, "info": info}


def _infer_ntop_input_entry(name: str, value: Any) -> Dict[str, Any]:
    """Infer an nTop JSON input entry from a Python value when no template is available."""
    if isinstance(value, dict) and "name" in value and "type" in value:
        return value
    if isinstance(value, bool):
        return {"name": name, "type": "bool", "values": value}
    if isinstance(value, int):
        return {"name": name, "type": "integer", "values": value}
    if isinstance(value, float):
        return {"name": name, "type": "scalar", "values": value}
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(isinstance(x, (int, float)) for x in value):
        return {"name": name, "type": "point", "values": [float(x) for x in value]}
    if isinstance(value, str):
        return {"name": name, "type": "text", "values": value}
    return {"name": name, "type": "text", "values": str(value)}


def build_input_payload(
    inputs: Dict[str, Any] | List[Dict[str, Any]],
    template: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize user-provided inputs into nTop Automate's JSON input schema.

    Accepts:
    - Full nTop schema dict: `{"inputs": [{"name": ..., "type": ..., "values": ...}, ...]}`
    - List of nTop input dicts: `[{"name": ..., "type": ..., "values": ...}, ...]`
    - Simple key-value mapping: `{"Length": 10.0, "WallThickness": 1.2}` (merged onto `template`
      if provided so types and units are preserved accurately).
    """
    if isinstance(inputs, list):
        return {"inputs": inputs}
    if isinstance(inputs, dict) and "inputs" in inputs and isinstance(inputs["inputs"], list):
        return inputs

    if not isinstance(inputs, dict):
        raise ValueError("inputs must be a dict or list of nTop input entries")

    if template and isinstance(template.get("inputs"), list):
        merged = json.loads(json.dumps(template))
        by_name = {item.get("name"): item for item in merged["inputs"] if "name" in item}
        for k, v in inputs.items():
            if k in by_name:
                entry = by_name[k]
                if isinstance(v, dict) and ("values" in v or "value" in v):
                    entry.update(v)
                else:
                    # nTop templates use "values" (or occasionally "value" for text/file)
                    if "value" in entry and "values" not in entry:
                        entry["value"] = v
                    else:
                        entry["values"] = v
            else:
                merged["inputs"].append(_infer_ntop_input_entry(k, v))
        return merged

    return {"inputs": [_infer_ntop_input_entry(k, v) for k, v in inputs.items()]}


class NTopRunner:
    """Wrapper around `ntopcl.exe` for headless template generation, execution, and sweeps."""

    def __init__(self, config: NTopConfig) -> None:
        self.config = config

    def _require_ntopcl(self) -> Path:
        if not self.config.ntopcl_path or not self.config.ntopcl_path.is_file():
            raise FileNotFoundError(
                "ntopcl executable not found. Set NTOP_CL_PATH environment variable or verify nTop installation."
            )
        return self.config.ntopcl_path

    def _auth_args(self) -> List[str]:
        args: List[str] = []
        if self.config.license_path:
            args.extend(["--license", self.config.license_path])
        if self.config.username:
            args.extend(["--username", self.config.username])
        if self.config.password:
            args.extend(["--password", self.config.password])
        if self.config.login_proxy:
            args.extend(["--loginproxy", self.config.login_proxy])
        return args

    def _library_args(self, extra_libs: Optional[Dict[str, str]] = None) -> List[str]:
        merged = dict(self.config.custom_block_libraries)
        if extra_libs:
            merged.update(extra_libs)
        args: List[str] = []
        for lib_name, lib_path in merged.items():
            args.extend(["--custom-block-library", f"{lib_name}:{lib_path}"])
        return args

    def generate_templates(
        self,
        notebook_path: str | Path,
        output_dir: Optional[str | Path] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run `ntopcl -t` to generate official input and output JSON templates."""
        exe = self._require_ntopcl()
        nb = self.config.resolve_path(str(notebook_path))
        if not nb.is_file():
            raise FileNotFoundError(f"Notebook not found: {nb}")

        cleanup_dir: Optional[tempfile.TemporaryDirectory] = None
        if output_dir is not None:
            run_dir = self.config.resolve_path(str(output_dir))
            run_dir.mkdir(parents=True, exist_ok=True)
        else:
            cleanup_dir = tempfile.TemporaryDirectory(prefix="ntop_tpl_")
            run_dir = Path(cleanup_dir.name)

        cmd = [str(exe), *self._auth_args(), *self._library_args(), "-t", str(nb)]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=timeout or self.config.default_timeout,
            )
            elapsed = round(time.perf_counter() - t0, 3)

            in_tpl_path = run_dir / "input_template.json"
            out_tpl_path = run_dir / "output_template.json"
            input_tpl = json.loads(in_tpl_path.read_text(encoding="utf-8")) if in_tpl_path.is_file() else None
            output_tpl = json.loads(out_tpl_path.read_text(encoding="utf-8")) if out_tpl_path.is_file() else None
            parsed_logs = parse_ntop_logs(proc.stdout, proc.stderr)

            return {
                "success": proc.returncode == 0 and input_tpl is not None,
                "exit_code": proc.returncode,
                "duration_seconds": elapsed,
                "notebook": str(nb),
                "input_template": input_tpl,
                "output_template": output_tpl,
                "input_template_path": str(in_tpl_path) if (output_dir is not None and in_tpl_path.is_file()) else None,
                "output_template_path": str(out_tpl_path) if (output_dir is not None and out_tpl_path.is_file()) else None,
                "errors": parsed_logs["errors"],
                "warnings": parsed_logs["warnings"],
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        finally:
            if cleanup_dir is not None:
                cleanup_dir.cleanup()

    def build_run_command(
        self,
        notebook_path: Path,
        input_json_path: Optional[Path] = None,
        output_json_path: Optional[Path] = None,
        include_properties: bool = False,
        save_notebook: bool = False,
        trust_notebook: bool = False,
        max_threads: Optional[int] = None,
        verbosity: int = 2,
        logfile: Optional[Path] = None,
        custom_block_libraries: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Build the `ntopcl` CLI command."""
        exe = self._require_ntopcl()
        cmd: List[str] = [str(exe)]
        cmd.extend(self._auth_args())
        cmd.extend(self._library_args(custom_block_libraries))

        if input_json_path is not None:
            cmd.extend(["-j", str(input_json_path)])
        if output_json_path is not None:
            cmd.extend(["-o", str(output_json_path)])
        if include_properties:
            cmd.append("-p")
        if save_notebook:
            cmd.append("-s")
        if trust_notebook:
            cmd.append("--trustnotebook")

        threads = max_threads if max_threads is not None else self.config.default_max_threads
        if threads is not None:
            cmd.extend(["-m", str(int(threads))])

        cmd.extend(["-v", str(int(verbosity))])

        if logfile is not None:
            cmd.append(f"--logfile={logfile}")

        cmd.append(str(notebook_path))
        return cmd

    def run_notebook(
        self,
        notebook_path: str | Path,
        inputs: Optional[Dict[str, Any] | List[Dict[str, Any]]] = None,
        input_json_path: Optional[str | Path] = None,
        output_json_path: Optional[str | Path] = None,
        working_dir: Optional[str | Path] = None,
        include_properties: bool = False,
        save_notebook: bool = False,
        trust_notebook: bool = False,
        max_threads: Optional[int] = None,
        verbosity: int = 2,
        custom_block_libraries: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
        auto_template: bool = True,
    ) -> Dict[str, Any]:
        """Execute an `.ntop` notebook headlessly via `ntopcl`."""
        nb = self.config.resolve_path(str(notebook_path))
        if not nb.is_file():
            raise FileNotFoundError(f"Notebook not found: {nb}")

        run_dir = self.config.resolve_path(str(working_dir)) if working_dir else nb.parent
        run_dir.mkdir(parents=True, exist_ok=True)

        temp_files: List[Path] = []
        resolved_in_json: Optional[Path] = None
        if input_json_path is not None:
            resolved_in_json = self.config.resolve_path(str(input_json_path))
            if not resolved_in_json.is_file():
                raise FileNotFoundError(f"Input JSON not found: {resolved_in_json}")
        elif inputs is not None:
            template = None
            if (
                auto_template
                and isinstance(inputs, dict)
                and "inputs" not in inputs
            ):
                tpl_res = self.generate_templates(nb)
                template = tpl_res.get("input_template")
            payload = build_input_payload(inputs, template=template)
            fd, tmp_in = tempfile.mkstemp(prefix="ntop_in_", suffix=".json", dir=str(run_dir))
            os.close(fd)
            resolved_in_json = Path(tmp_in)
            resolved_in_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_files.append(resolved_in_json)

        resolved_out_json: Optional[Path] = None
        cleanup_out = False
        if output_json_path is not None:
            resolved_out_json = self.config.resolve_path(str(output_json_path))
            resolved_out_json.parent.mkdir(parents=True, exist_ok=True)
        else:
            fd, tmp_out = tempfile.mkstemp(prefix="ntop_out_", suffix=".json", dir=str(run_dir))
            os.close(fd)
            resolved_out_json = Path(tmp_out)
            resolved_out_json.unlink(missing_ok=True)
            cleanup_out = True

        cmd = self.build_run_command(
            notebook_path=nb,
            input_json_path=resolved_in_json,
            output_json_path=resolved_out_json,
            include_properties=include_properties,
            save_notebook=save_notebook,
            trust_notebook=trust_notebook,
            max_threads=max_threads,
            verbosity=verbosity,
            custom_block_libraries=custom_block_libraries,
        )

        # Snapshot existing files in run_dir to detect newly generated artifacts
        before_mtime = {p.name: p.stat().st_mtime for p in run_dir.iterdir() if p.is_file()}
        t0 = time.perf_counter()
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code = -1

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=timeout_seconds or self.config.default_timeout,
            )
            exit_code = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        finally:
            elapsed = round(time.perf_counter() - t0, 3)

        output_data = None
        if resolved_out_json and resolved_out_json.is_file() and resolved_out_json.stat().st_size > 0:
            try:
                output_data = json.loads(resolved_out_json.read_text(encoding="utf-8"))
            except Exception:
                output_data = resolved_out_json.read_text(encoding="utf-8", errors="replace")

        created_or_updated: List[str] = []
        for p in run_dir.iterdir():
            if not p.is_file():
                continue
            if p in temp_files or (cleanup_out and p == resolved_out_json):
                continue
            prev = before_mtime.get(p.name)
            if prev is None or p.stat().st_mtime > prev:
                created_or_updated.append(str(p))

        for tf in temp_files:
            tf.unlink(missing_ok=True)
        if cleanup_out and resolved_out_json:
            resolved_out_json.unlink(missing_ok=True)

        parsed_logs = parse_ntop_logs(stdout, stderr)
        return {
            "success": (not timed_out) and exit_code == 0,
            "timed_out": timed_out,
            "exit_code": exit_code,
            "duration_seconds": elapsed,
            "notebook": str(nb),
            "working_dir": str(run_dir),
            "output_json_path": str(resolved_out_json) if (not cleanup_out and resolved_out_json) else None,
            "output": output_data,
            "generated_files": sorted(created_or_updated),
            "errors": parsed_logs["errors"],
            "warnings": parsed_logs["warnings"],
            "stdout": stdout.strip()[-8000:],
            "stderr": stderr.strip()[-4000:],
        }

    def run_parameter_sweep(
        self,
        notebook_path: str | Path,
        parameter_sets: Optional[List[Dict[str, Any]]] = None,
        parameter_grid: Optional[Dict[str, List[Any]]] = None,
        output_dir: Optional[str | Path] = None,
        max_workers: int = 1,
        max_threads_per_run: Optional[int] = None,
        include_properties: bool = False,
        trust_notebook: bool = False,
        timeout_per_run: Optional[int] = None,
        export_csv: bool = True,
    ) -> Dict[str, Any]:
        """Run a batch DOE / parameter sweep across multiple input sets."""
        nb = self.config.resolve_path(str(notebook_path))
        if not nb.is_file():
            raise FileNotFoundError(f"Notebook not found: {nb}")

        cases: List[Dict[str, Any]] = []
        if parameter_sets:
            cases = list(parameter_sets)
        elif parameter_grid:
            keys = list(parameter_grid.keys())
            value_lists = [parameter_grid[k] for k in keys]
            for combo in itertools.product(*value_lists):
                cases.append(dict(zip(keys, combo)))
        else:
            raise ValueError("Provide either parameter_sets or parameter_grid")

        if not cases:
            raise ValueError("No parameter cases to run")

        sweep_dir = (
            self.config.resolve_path(str(output_dir))
            if output_dir
            else (nb.parent / f"{nb.stem}_sweep_{time.strftime('%Y%m%d_%H%M%S')}")
        )
        sweep_dir.mkdir(parents=True, exist_ok=True)

        # Fetch template once for fast mapping across all cases
        tpl_res = self.generate_templates(nb)
        template = tpl_res.get("input_template")

        def _execute_case(idx: int, params: Dict[str, Any]) -> Dict[str, Any]:
            case_dir = sweep_dir / f"run_{idx:03d}"
            case_dir.mkdir(parents=True, exist_ok=True)
            in_json = case_dir / "input.json"
            out_json = case_dir / "output.json"

            payload = build_input_payload(params, template=template)
            in_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

            res = self.run_notebook(
                notebook_path=nb,
                input_json_path=in_json,
                output_json_path=out_json,
                working_dir=case_dir,
                include_properties=include_properties,
                trust_notebook=trust_notebook,
                max_threads=max_threads_per_run,
                timeout_seconds=timeout_per_run,
                auto_template=False,
            )
            return {
                "run_index": idx,
                "run_dir": str(case_dir),
                "inputs": params,
                "success": res["success"],
                "exit_code": res["exit_code"],
                "duration_seconds": res["duration_seconds"],
                "output": res["output"],
                "generated_files": res["generated_files"],
                "errors": res["errors"],
            }

        t0 = time.perf_counter()
        results: List[Optional[Dict[str, Any]]] = [None] * len(cases)
        workers = max(1, min(int(max_workers), len(cases)))

        if workers == 1:
            for i, case in enumerate(cases):
                results[i] = _execute_case(i, case)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {pool.submit(_execute_case, i, case): i for i, case in enumerate(cases)}
                for fut in as_completed(future_map):
                    idx = future_map[fut]
                    results[idx] = fut.result()

        final_results = [r for r in results if r is not None]
        csv_path: Optional[Path] = None
        if export_csv:
            csv_path = sweep_dir / "sweep_summary.csv"
            self._write_sweep_csv(csv_path, final_results)

        succeeded = sum(1 for r in final_results if r["success"])
        return {
            "notebook": str(nb),
            "sweep_dir": str(sweep_dir),
            "total_runs": len(final_results),
            "succeeded": succeeded,
            "failed": len(final_results) - succeeded,
            "total_duration_seconds": round(time.perf_counter() - t0, 3),
            "summary_csv": str(csv_path) if csv_path else None,
            "runs": final_results,
        }

    @staticmethod
    def _write_sweep_csv(csv_path: Path, runs: List[Dict[str, Any]]) -> None:
        rows: List[Dict[str, Any]] = []
        fieldnames: List[str] = ["run_index", "success", "exit_code", "duration_seconds"]
        seen = set(fieldnames)

        for r in runs:
            row: Dict[str, Any] = {
                "run_index": r["run_index"],
                "success": r["success"],
                "exit_code": r["exit_code"],
                "duration_seconds": r["duration_seconds"],
            }
            for k, v in (r.get("inputs") or {}).items():
                col = f"in_{k}"
                row[col] = json.dumps(v) if isinstance(v, (dict, list)) else v
                if col not in seen:
                    seen.add(col)
                    fieldnames.append(col)

            out = r.get("output")
            if isinstance(out, dict):
                # Handle both list-of-outputs or flat dict
                if isinstance(out.get("outputs"), list):
                    for idx, item in enumerate(out["outputs"]):
                        name = item.get("name") or f"out_{idx}"
                        col = f"out_{name}"
                        val = item.get("value", item.get("values"))
                        row[col] = json.dumps(val) if isinstance(val, (dict, list)) else val
                        if col not in seen:
                            seen.add(col)
                            fieldnames.append(col)
                else:
                    for k, v in out.items():
                        col = f"out_{k}"
                        row[col] = json.dumps(v) if isinstance(v, (dict, list)) else v
                        if col not in seen:
                            seen.add(col)
                            fieldnames.append(col)
            rows.append(row)

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def evaluate_block(
        self,
        notebook_path: str | Path,
        target: int | str,
        inputs: Optional[Dict[str, Any] | List[Dict[str, Any]]] = None,
        include_properties: bool = True,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Non-invasively evaluate any block in a notebook by running a temporary copy via `ntopcl`."""
        nb_src = self.config.resolve_path(str(notebook_path))
        if not nb_src.is_file():
            raise FileNotFoundError(f"Notebook not found: {nb_src}")

        with tempfile.TemporaryDirectory(prefix="ntop_eval_") as tmp:
            tmp_dir = Path(tmp)
            tmp_nb = tmp_dir / nb_src.name
            nb = NTopNotebook.load(nb_src)
            out_info = nb.set_output(target)
            nb.save(tmp_nb)

            res = self.run_notebook(
                notebook_path=tmp_nb,
                inputs=inputs,
                working_dir=tmp_dir,
                include_properties=include_properties,
                verbosity=1,
                timeout_seconds=timeout_seconds,
            )

            # Extract convenient bounding box / scalar summary when present
            bbox = None
            scalar_value = None
            out = res.get("output")
            if isinstance(out, list) and out:
                first = out[0]
                if isinstance(first, dict):
                    if first.get("type") in ("real", "integer", "bool", "text"):
                        scalar_value = first.get("value")
                    for prop in first.get("properties", []) or []:
                        if isinstance(prop, dict) and prop.get("name") == "bounding box":
                            bbox = prop.get("value")

            return {
                "notebook": str(nb_src),
                "evaluated_block": out_info,
                "success": res["success"],
                "exit_code": res["exit_code"],
                "duration_seconds": res["duration_seconds"],
                "bounding_box": bbox,
                "scalar_value": scalar_value,
                "output": out,
                "errors": res["errors"],
                "warnings": res["warnings"],
            }

    def export_block_mesh(
        self,
        notebook_path: str | Path,
        target: int | str,
        export_path: str | Path,
        tolerance: Any = "1mm",
        unit: str = "mm",
        sharpen: bool = False,
        simplify: bool = False,
        inputs: Optional[Dict[str, Any] | List[Dict[str, Any]]] = None,
        analyze: bool = True,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Non-invasively mesh an implicit/mesh block and export it to `.stl`, `.3mf`, or `.obj` via `ntopcl`.

        Never modifies `notebook_path`; appends `implicit_to_mesh` + `export_mesh` onto a temporary copy,
        runs `ntopcl`, and optionally analyzes the exported mesh (volume, surface area, watertightness).
        """
        nb_src = self.config.resolve_path(str(notebook_path))
        if not nb_src.is_file():
            raise FileNotFoundError(f"Notebook not found: {nb_src}")

        dest = self.config.resolve_path(str(export_path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() not in (".stl", ".3mf", ".obj", ".ply"):
            raise ValueError(f"Unsupported mesh export extension {dest.suffix!r}. Use .stl, .3mf, .obj, or .ply")

        with tempfile.TemporaryDirectory(prefix="ntop_export_") as tmp:
            tmp_dir = Path(tmp)
            tmp_nb = tmp_dir / nb_src.name
            nb = NTopNotebook.load(nb_src)
            blk = nb._resolve_block(target)
            blk_id = blk["id"]
            blk_type = blk.get("type", "")

            if blk_type == "mesh":
                mesh_id = blk_id
            else:
                tol_id = nb.add_literal("real", tolerance, units={"length": 1})["block_id"]
                sharp_id = nb.add_literal("bool", bool(sharpen))["block_id"]
                simp_id = nb.add_literal("bool", bool(simplify))["block_id"]
                mesh_id = nb.add_block(
                    func="implicit_to_mesh<implicit,real,real,bool,bool>[2.5.0]",
                    block_type="mesh",
                    inputs=[
                        {"instanceId": blk_id},
                        {"instanceId": tol_id},
                        {"instanceId": 0, "is_default": True},
                        {"instanceId": sharp_id, "is_default": True},
                        {"instanceId": simp_id, "is_default": True},
                    ],
                    name="MCP_Temp_Mesh",
                )["block_id"]
                # Mark optional feature size input slot with type="real" as nTop expects
                bmap = nb._block_map()
                bmap[mesh_id]["inputs"][2]["type"] = "real"

            fp_id = nb.add_literal("file_path", dest.as_posix())["block_id"]
            unit_id = nb.add_literal("unit_length_enum", {"id": unit})["block_id"]
            export_id = nb.add_block(
                func="export_mesh<file_path,mesh,unit_length_enum>",
                block_type="mesh_file_data",
                inputs=[
                    {"instanceId": fp_id},
                    {"instanceId": mesh_id},
                    {"instanceId": unit_id, "is_default": True},
                ],
                name="MCP_Temp_Export",
            )["block_id"]

            nb.set_output(export_id)
            nb.save(tmp_nb)

            res = self.run_notebook(
                notebook_path=tmp_nb,
                inputs=inputs,
                working_dir=tmp_dir,
                include_properties=False,
                verbosity=1,
                timeout_seconds=timeout_seconds,
            )

            exported_ok = dest.is_file() and dest.stat().st_size > 0
            mesh_stats = None
            if exported_ok and analyze and dest.suffix.lower() in (".stl", ".obj", ".3mf"):
                mesh_stats = analyze_mesh_file(dest)

            return {
                "notebook": str(nb_src),
                "source_block": {"id": blk_id, "name": blk.get("name", ""), "type": blk_type},
                "export_path": str(dest),
                "exported": exported_ok,
                "file_size_bytes": dest.stat().st_size if exported_ok else 0,
                "success": res["success"] and exported_ok,
                "duration_seconds": res["duration_seconds"],
                "mesh_analysis": mesh_stats,
                "errors": res["errors"],
                "warnings": res["warnings"],
            }


# ----------------------------------------------------------------------
# Background Async Job Manager
# ----------------------------------------------------------------------


@dataclass
class BackgroundJob:
    job_id: str
    notebook: str
    command: List[str]
    working_dir: str
    output_json_path: Optional[str]
    status: str = "running"  # running | completed | failed | cancelled
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    stdout_lines: List[str] = field(default_factory=list)
    stderr_lines: List[str] = field(default_factory=list)
    output_data: Any = None
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _temp_files: List[Path] = field(default_factory=list, repr=False)

    def to_summary(self, tail_lines: int = 40) -> Dict[str, Any]:
        now = self.finished_at or time.time()
        stdout_text = "\n".join(self.stdout_lines)
        stderr_text = "\n".join(self.stderr_lines)
        parsed = parse_ntop_logs(stdout_text, stderr_text)
        return {
            "job_id": self.job_id,
            "notebook": self.notebook,
            "status": self.status,
            "exit_code": self.exit_code,
            "elapsed_seconds": round(now - self.started_at, 3),
            "working_dir": self.working_dir,
            "output_json_path": self.output_json_path,
            "output": self.output_data,
            "errors": parsed["errors"],
            "warnings": parsed["warnings"],
            "stdout_tail": self.stdout_lines[-tail_lines:],
            "stderr_tail": self.stderr_lines[-tail_lines:],
        }


class JobManager:
    """Manages asynchronous background `ntopcl` jobs."""

    def __init__(self, runner: NTopRunner) -> None:
        self.runner = runner
        self._jobs: Dict[str, BackgroundJob] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        notebook_path: str | Path,
        inputs: Optional[Dict[str, Any] | List[Dict[str, Any]]] = None,
        input_json_path: Optional[str | Path] = None,
        output_json_path: Optional[str | Path] = None,
        working_dir: Optional[str | Path] = None,
        include_properties: bool = False,
        save_notebook: bool = False,
        trust_notebook: bool = False,
        max_threads: Optional[int] = None,
        verbosity: int = 2,
        custom_block_libraries: Optional[Dict[str, str]] = None,
    ) -> BackgroundJob:
        nb = self.runner.config.resolve_path(str(notebook_path))
        if not nb.is_file():
            raise FileNotFoundError(f"Notebook not found: {nb}")

        run_dir = self.runner.config.resolve_path(str(working_dir)) if working_dir else nb.parent
        run_dir.mkdir(parents=True, exist_ok=True)

        job_id = f"ntop_{uuid.uuid4().hex[:8]}"
        temp_files: List[Path] = []
        resolved_in: Optional[Path] = None

        if input_json_path is not None:
            resolved_in = self.runner.config.resolve_path(str(input_json_path))
        elif inputs is not None:
            tpl = None
            if isinstance(inputs, dict) and "inputs" not in inputs:
                tpl = self.runner.generate_templates(nb).get("input_template")
            payload = build_input_payload(inputs, template=tpl)
            resolved_in = run_dir / f"{job_id}_input.json"
            resolved_in.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_files.append(resolved_in)

        resolved_out = (
            self.runner.config.resolve_path(str(output_json_path))
            if output_json_path
            else (run_dir / f"{job_id}_output.json")
        )
        resolved_out.parent.mkdir(parents=True, exist_ok=True)

        cmd = self.runner.build_run_command(
            notebook_path=nb,
            input_json_path=resolved_in,
            output_json_path=resolved_out,
            include_properties=include_properties,
            save_notebook=save_notebook,
            trust_notebook=trust_notebook,
            max_threads=max_threads,
            verbosity=verbosity,
            custom_block_libraries=custom_block_libraries,
        )

        proc = subprocess.Popen(
            cmd,
            cwd=str(run_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        job = BackgroundJob(
            job_id=job_id,
            notebook=str(nb),
            command=cmd,
            working_dir=str(run_dir),
            output_json_path=str(resolved_out),
            process=proc,
            _temp_files=temp_files,
        )
        with self._lock:
            self._jobs[job_id] = job

        def _watch() -> None:
            def _drain(stream, sink: List[str]) -> None:
                if stream is None:
                    return
                for line in iter(stream.readline, ""):
                    sink.append(line.rstrip("\r\n"))
                stream.close()

            t_out = threading.Thread(target=_drain, args=(proc.stdout, job.stdout_lines), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, job.stderr_lines), daemon=True)
            t_out.start()
            t_err.start()
            rc = proc.wait()
            t_out.join(timeout=5)
            t_err.join(timeout=5)

            job.exit_code = rc
            job.finished_at = time.time()
            if job.status != "cancelled":
                job.status = "completed" if rc == 0 else "failed"

            if resolved_out.is_file() and resolved_out.stat().st_size > 0:
                try:
                    job.output_data = json.loads(resolved_out.read_text(encoding="utf-8"))
                except Exception:
                    job.output_data = resolved_out.read_text(encoding="utf-8", errors="replace")

        threading.Thread(target=_watch, daemon=True).start()
        return job

    def get(self, job_id: str) -> BackgroundJob:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"Job {job_id!r} not found")
            return self._jobs[job_id]

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [
            {
                "job_id": j.job_id,
                "notebook": j.notebook,
                "status": j.status,
                "exit_code": j.exit_code,
                "elapsed_seconds": round((j.finished_at or time.time()) - j.started_at, 3),
            }
            for j in jobs
        ]

    def cancel(self, job_id: str) -> Dict[str, Any]:
        job = self.get(job_id)
        if job.status != "running" or job.process is None:
            return {"job_id": job_id, "cancelled": False, "status": job.status}
        job.status = "cancelled"
        job.process.terminate()
        try:
            job.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            job.process.kill()
        job.finished_at = time.time()
        return {"job_id": job_id, "cancelled": True, "status": job.status}
