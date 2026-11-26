"""Built-in knowledge base and best-practice guides for nTop & nTop Automate."""

from __future__ import annotations

from typing import Dict


GUIDES: Dict[str, str] = {
    "ntopcl_cli": """# nTop Automate (ntopcl) CLI Reference

Basic syntax:
```bash
ntopcl [options] <path/to/notebook.ntop>
```

Common options:
- `-t, --template`: Generate `input_template.json` and `output_template.json` in the current working directory.
- `-j, --json <file>`: Pass an input JSON file matching the notebook's Inputs section.
- `-o, --output <file>`: Write computed Output section values to a JSON file.
- `-p, --properties`: Include top-level properties (e.g., bounding box, mass properties) in the output JSON.
- `-s, --save`: Overwrite the `.ntop` file with computed results after execution.
- `-v, --verbose <0|1|2>`: 0 = errors only, 1 = warnings + errors (default), 2 = warnings + errors + runtime progress.
- `-m, --maxthreads <N>`: Limit parallel CPU threads (useful during parallel parameter sweeps).
- `--trustnotebook`: Allow execution of `Run Command` blocks inside the notebook.
- `--custom-block-library <name:path>`: Register custom block library folders (repeatable).
- `--logfile[=path]`: Mirror console output to a log file.
- `--license <path>` / `-u <user> -w <pass>`: Explicit license file or cloud login credentials.
""",
    "json_inputs": """# nTop Automate JSON Input Format

Run `generate_templates` (`ntopcl -t`) first to inspect the exact schema expected by your notebook.

Example `input.json`:
```json
{
  "inputs": [
    {
      "name": "Cell Size",
      "type": "scalar",
      "values": 5.0,
      "units": "mm"
    },
    {
      "name": "Wall Thickness",
      "type": "scalar",
      "values": 0.8,
      "units": "mm"
    },
    {
      "name": "Lattice Type",
      "type": "enum",
      "values": 5
    },
    {
      "name": "Export Path",
      "type": "file_path",
      "values": "E:\\\\Ntop\\\\exports\\\\part.stl"
    }
  ]
}
```

Supported input types: `bool`, `integer`, `scalar` (real), `text`, `file_path`, `point`, `vector`, `enum`, and lists thereof.
Tip: When using this MCP server's `run_notebook` tool, you can pass either the full `{"inputs": [...]}` schema OR a simple dictionary like `{"Cell Size": 5.0, "Wall Thickness": 0.8}` — the server automatically merges it with the notebook's generated template!
""",
    "json_outputs": """# nTop Automate JSON Output Format

When a notebook has a valid **Output** block assigned (either in the nTop GUI or via `set_notebook_output`), `ntopcl -o output.json` writes the evaluated output to JSON.

- Scalar / Integer / Text / Bool outputs appear directly with their values and units.
- Geometry / Export blocks (e.g., `Export Mesh`, `Export CAD`, `Export Part to 3MF`) write geometry files to disk and return paths/metadata.
- Use `include_properties=True` (`-p`) to include top-level properties (bounding box, mass, surface area, etc.) in the JSON output.
- After running, use `analyze_mesh` on exported `.stl` / `.obj` / `.3mf` files or `read_export_table` on `.csv` outputs.
""",
    "notebook_preparation": """# Preparing an `.ntop` Notebook for Headless Automation

1. **Promote Parameters to Inputs:** Drag parameters you want to vary into the **Inputs** section at the top of the nTop Notebook.
2. **Assign an Output Block:** Drag the final result (scalar, list, JSON/export block, or implicit/mesh body) into the **Output** section.
   - If you forgot to set an Output block in the GUI, you can inspect the graph with `read_notebook_graph` and assign one directly via `set_notebook_output`!
3. **Use Relative or Parameterized Export Paths:** Expose the export directory or file path as a `Text` / `File Path` input so parameter sweeps don't overwrite each other.
4. **Validate Before Sweeping:** Run `validate_notebook_graph` and `generate_templates` first, then run a single `run_notebook` test before launching a multi-case `run_parameter_sweep`.
5. **Internal SI Units Note:** Inside `.ntop` binary files, `real` leaf literals are stored in SI base units (meters for length, radians for angle), while `ntopcl` JSON inputs honor explicit `"units"` strings (e.g. `"mm"`).
""",
    "parameter_sweep": """# Parameter Sweeps & DOE Best Practices

Use `run_parameter_sweep` to explore design spaces automatically:
- Pass `parameter_grid={"CellSize": [4.0, 5.0, 6.0], "Thickness": [0.6, 0.8]}` for a full Cartesian sweep (6 runs), OR
- Pass `parameter_sets=[{...}, {...}]` for Latin Hypercube / custom DOE points.
- Each run executes in its own isolated `run_000/`, `run_001/`, ... subfolder with saved `input.json` and `output.json`.
- When using `max_workers > 1`, set `max_threads_per_run` (e.g. `total_cores // max_workers`) to avoid CPU oversubscription.
- A consolidated `sweep_summary.csv` is automatically generated in the sweep folder.
""",
    "troubleshooting": """# Troubleshooting nTop Automate & MCP Issues

- **`Error generating output template : Output of function not set`**:
  The notebook has `output == -1`. Open in nTop GUI and drag a block into the Output section, or call `set_notebook_output(notebook_path, block_id, output_path=...)`.
- **License / Authentication errors**:
  Ensure nTop is signed in locally, or set `NTOP_LICENSE` (path to `.lic`) or `NTOP_USERNAME` + `NTOP_PASSWORD` in your MCP environment variables.
- **`Run Command` block blocked**:
  Pass `trust_notebook=True` (`--trustnotebook`) if your notebook intentionally executes external commands.
- **Custom Block not found**:
  Pass `custom_block_libraries={"MyLib": "C:/path/to/lib"}` or configure custom block paths.
- **Mesh not watertight after export**:
  Use `analyze_mesh` to inspect `open_boundary_edges` and `non_manifold_edges`; tighten tolerance or add a `Remesh Surface` / `Mesh from Implicit Body` cleanup step in the notebook.
""",
}


def get_guide(topic: str) -> Dict[str, object]:
    key = topic.strip().lower()
    if key in ("all", "list", "index"):
        return {
            "available_topics": list(GUIDES.keys()),
            "description": "Pass any topic name to get_ntop_guide to read the guide.",
        }
    if key not in GUIDES:
        return {
            "error": f"Unknown topic {topic!r}",
            "available_topics": list(GUIDES.keys()),
        }
    return {
        "topic": key,
        "content": GUIDES[key],
    }
