# nTop MCP Server (`ntop-mcp`)

A Model Context Protocol (MCP) server for **nTop (nTopology)** combining lossless `.ntop` block graph editing, transactional authoring, local block-signature discovery, headless `ntopcl` execution/sweeps, GUI conflict safety, and mesh export/analysis (bridging cleanly with COMSOL & Abaqus workflows).

## Highlights

1. **Direct `.ntop` Container & Transactional Block Graph Authoring**
   - Lossless binary reader/writer for `MAGIC%$1` / `MAGIC@@9` `.ntop` files.
   - Human-friendly unit parsing (`"10mm"`, `"2.5cm"`, `"45deg"`, `{"value": 10, "unit": "mm"}`) automatically converted to SI base units + GUI expression strings.
   - **`batch_edit_notebook`**: Apply dozens of graph edits atomically using `$alias` references, with graph validation and optional `ntopcl` pre-flight verification before writing to disk.
   - **`set_block_visibility`**: Control viewport show/hide, `solo=True` spotlighting, color (`#RRGGBB` / RGBA), transparency, and opacity.
   - **`promote_to_input`**: Turn GUI variables into Notebook Inputs (`def.inputs`) ready for `ntopcl -j` and `run_parameter_sweep`.
   - **Multi-version Snapshots & Undo**: Automatic timestamped backups under `.ntop_history/` with `list_notebook_snapshots` and `undo_notebook_edit`.
   - **GUI Conflict Safety & Auto-Reload**: Detects running `ntop.exe` instances holding the target file, guards against unsaved GUI edits via nTop's Autosave timestamps, prevents accidental double-window launches (`open_in_ntop_gui`), and supports `auto_reload_gui=True` on edit/undo tools to safely refresh the GUI window in one step.
   - **In-Place Custom Block Hot-Updates (`package_custom_block`)**: Packages any notebook branch into a reusable Custom Block `.ntop` while preserving its `def.function` UUID across updates, enabling nTop GUI's native `File -> Import -> Custom Block -> Update` workflow without closing the parent notebook.

2. **Verified Block Signature Catalog**
   - **`build_block_catalog`** scans any folder of `.ntop` notebooks to build a local nTop 5.x signature database (`block_catalog.json`).
   - **`search_blocks`** searches built-in + locally scanned signatures with exact version suffixes and input slot types.

3. **nTop Automate (`ntopcl`), Non-Invasive Evaluation & Mesh Export**
   - **`evaluate_block`**: Inspect any block's evaluated value or bounding box on a temp copy without modifying the original `.ntop`.
   - **`export_block_mesh`**: Non-invasively convert an implicit/mesh block to `.stl`, `.3mf`, or `.obj` via `ntopcl` and automatically inspect volume, surface area, bounding box, and watertightness.
   - **`generate_templates`**, **`run_notebook`**, **`run_notebook_async`** (+ job control), and **`run_parameter_sweep`** (Cartesian grid or DOE sets with CSV summary).

## MCP Tools (36 Tools)

- **Environment & GUI**: `check_ntop_environment`, `set_workdir`, `list_ntop_files`, `open_in_ntop_gui`
- **Inspection & Validation**: `inspect_notebook`, `list_notebook_variables`, `read_notebook_graph`, `validate_notebook_graph`
- **Direct Graph Editing, Custom Blocks & Snapshots**: `set_notebook_literal`, `set_notebook_output`, `rewire_block_input`, `prune_notebook_graph`, `add_notebook_section`, `add_notebook_literal`, `add_notebook_block`, `add_notebook_variable`, `batch_edit_notebook`, `set_block_visibility`, `promote_to_input`, `package_custom_block`, `list_notebook_snapshots`, `undo_notebook_edit`
- **Block Signature Catalog**: `build_block_catalog`, `search_blocks`
- **Non-Invasive Evaluation & Export**: `evaluate_block`, `export_block_mesh`
- **nTop Automate & Sweeps**: `generate_templates`, `run_notebook`, `run_notebook_async`, `get_job_status`, `list_jobs`, `cancel_job`, `run_parameter_sweep`
- **Mesh & Table Analysis**: `analyze_mesh`, `read_export_table`
- **Documentation**: `get_ntop_guide`

## Quick Start on Any PC

### 1. Clone & Install

Requires **Python >= 3.10** and a local installation of **nTop** (`ntopcl.exe` / `ntop.exe`).

```bash
git clone https://github.com/heihei-ok/nTop-MCP-Server.git
cd nTop-MCP-Server
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
pip install -e .
```

### 2. Configure Your MCP Client

Add `ntop` to your MCP config (`mcp_config.json`, Claude Desktop, Cursor, Antigravity, etc.):

```json
{
  "mcpServers": {
    "ntop": {
      "command": "C:\\path\\to\\ntop-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "ntop_mcp.server"],
      "env": {
        "NTOP_CL_PATH": "C:\\Program Files\\nTopology\\nTopology\\ntopcl.exe",
        "NTOP_GUI_PATH": "C:\\Program Files\\nTopology\\nTopology\\ntop.exe",
        "NTOP_WORKDIR": "C:\\path\\to\\your\\ntop\\projects"
      }
    },
    "ntop-docs": {
      "url": "https://docs.ntop.com/mcp"
    }
  }
}
```

> `NTOP_CL_PATH` and `NTOP_GUI_PATH` are optional if `ntopcl.exe` is already in `PATH`, `D:\NTOP`, or `C:\Program Files\nTopology\nTopology`.

### 3. Build Your Local Block Signature Catalog (Recommended)

Once connected, run `build_block_catalog` on a folder containing your local `.ntop` examples or custom block libraries to index verified block signatures for your exact installed nTop version.

### 4. Run Tests

```bash
pip install -e ".[dev]"
pytest -v tests
```

Optional live `ntopcl` integration tests can be enabled via environment variables: `NTOP_TEST_SAMPLE`, `NTOP_TEST_TORUS`, `NTOP_TEST_HEX`.
