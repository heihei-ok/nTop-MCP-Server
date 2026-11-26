"""Block signature catalog for nTop 5.x (built-in essentials + local `.ntop` directory scanner)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .graph import NTopNotebook


DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "block_catalog.json"

# Curated verified nTop 5.49.2 block signatures always available out-of-the-box
BUILTIN_BLOCKS: List[Dict[str, Any]] = [
    {
        "func": "boolean_union<blend_enum,real_field,list<implicit>>[5.44.0]",
        "category": "modeling",
        "output_type": "implicit",
        "description": "Boolean Union of a list of implicit bodies with optional blend radius.",
        "inputs": [
            {"index": 0, "name": "Blend type", "type": "blend_enum", "default": 0},
            {"index": 1, "name": "Blend radius", "type": "real", "default": "0mm"},
            {"index": 2, "name": "Bodies", "type": "list<implicit>"},
        ],
    },
    {
        "func": "boolean_subtract<blend_enum,real_field,implicit,list<implicit>>[5.44.0]",
        "category": "modeling",
        "output_type": "implicit",
        "description": "Boolean Subtract: remove a list of bodies from a primary implicit body.",
        "inputs": [
            {"index": 0, "name": "Blend type", "type": "blend_enum", "default": 0},
            {"index": 1, "name": "Blend radius", "type": "real", "default": "0mm"},
            {"index": 2, "name": "Primary Body", "type": "implicit"},
            {"index": 3, "name": "Subtraction Bodies", "type": "list<implicit>"},
        ],
    },
    {
        "func": "boolean_intersect<blend_enum,real_field,list<implicit>>[5.44.0]",
        "category": "modeling",
        "output_type": "implicit",
        "description": "Boolean Intersect of a list of implicit bodies (also supports plane half-spaces).",
        "inputs": [
            {"index": 0, "name": "Blend type", "type": "blend_enum", "default": 0},
            {"index": 1, "name": "Blend radius", "type": "real", "default": "0mm"},
            {"index": 2, "name": "Bodies", "type": "list<implicit>"},
        ],
    },
    {
        "func": "shell<implicit,real_field,shell_enum>[5.24.0]",
        "category": "modeling",
        "output_type": "implicit",
        "description": "Shell an implicit body inward (0), outward (1), or mid-surface (2).",
        "inputs": [
            {"index": 0, "name": "Body", "type": "implicit"},
            {"index": 1, "name": "Thickness", "type": "real_field"},
            {"index": 2, "name": "Direction", "type": "shell_enum", "default": 1},
        ],
    },
    {
        "func": "thicken_implicit<implicit,real_field>",
        "category": "modeling",
        "output_type": "implicit",
        "description": "Thicken an implicit body or distance field by a scalar/field thickness.",
        "inputs": [
            {"index": 0, "name": "Body / Field", "type": "implicit"},
            {"index": 1, "name": "Thickness", "type": "real_field"},
        ],
    },
    {
        "func": "plane_from_normal<point,vector>[1.1.0]",
        "category": "geometry",
        "output_type": "plane",
        "description": "Create a Plane from an origin point and normal vector.",
        "inputs": [
            {"index": 0, "name": "Origin", "type": "point"},
            {"index": 1, "name": "Normal", "type": "vector"},
        ],
    },
    {
        "func": "implicit_to_mesh<implicit,real,real,bool,bool>[2.5.0]",
        "category": "meshing",
        "output_type": "mesh",
        "description": "Convert an Implicit Body to a surface Mesh with given tolerance.",
        "inputs": [
            {"index": 0, "name": "Implicit Body", "type": "implicit"},
            {"index": 1, "name": "Tolerance", "type": "real", "default": "1mm"},
            {"index": 2, "name": "Feature size (optional)", "type": "real", "default": None},
            {"index": 3, "name": "Sharpen", "type": "bool", "default": False},
            {"index": 4, "name": "Simplify", "type": "bool", "default": False},
        ],
    },
    {
        "func": "export_mesh<file_path,mesh,unit_length_enum>",
        "category": "export",
        "output_type": "mesh_file_data",
        "description": "Export a surface Mesh to .stl, .3mf, .obj, or .ply.",
        "inputs": [
            {"index": 0, "name": "Path", "type": "file_path"},
            {"index": 1, "name": "Mesh", "type": "mesh"},
            {"index": 2, "name": "Units", "type": "unit_length_enum", "default": {"id": "mm"}},
        ],
    },
    {
        "func": "core.list<implicit>",
        "category": "core",
        "output_type": "list<implicit>",
        "description": "Construct a list of implicit bodies (variadic inputs).",
        "inputs": [{"index": 0, "name": "Item 0", "type": "implicit"}],
    },
    {
        "func": "core.list<plane>",
        "category": "core",
        "output_type": "list<plane>",
        "description": "Construct a list of planes (variadic inputs).",
        "inputs": [{"index": 0, "name": "Item 0", "type": "plane"}],
    },
    {
        "func": "add<real,real>",
        "category": "math",
        "output_type": "real",
        "description": "Add two scalar values.",
        "inputs": [
            {"index": 0, "name": "A", "type": "real"},
            {"index": 1, "name": "B", "type": "real"},
        ],
    },
    {
        "func": "subtract<real,real>",
        "category": "math",
        "output_type": "real",
        "description": "Subtract scalar B from scalar A.",
        "inputs": [
            {"index": 0, "name": "A", "type": "real"},
            {"index": 1, "name": "B", "type": "real"},
        ],
    },
    {
        "func": "multiply<real,real>",
        "category": "math",
        "output_type": "real",
        "description": "Multiply two scalar values.",
        "inputs": [
            {"index": 0, "name": "A", "type": "real"},
            {"index": 1, "name": "B", "type": "real"},
        ],
    },
    {
        "func": "divide<real,real>",
        "category": "math",
        "output_type": "real",
        "description": "Divide scalar A by scalar B.",
        "inputs": [
            {"index": 0, "name": "A", "type": "real"},
            {"index": 1, "name": "B", "type": "real"},
        ],
    },
]


def _classify_func(func: str) -> str:
    fl = func.lower()
    if fl.startswith("user_func_"):
        return "custom_block"
    if fl.startswith("core."):
        return "core"
    if any(k in fl for k in ("export_", "import_")):
        return "export_import"
    if any(k in fl for k in ("mesh", "remesh", "fe_", "simulation", "static_analysis", "boundary", "domain")):
        return "meshing_and_fe"
    if any(k in fl for k in ("boolean_", "shell", "thicken", "offset", "fillet", "mirror", "translate", "rotate")):
        return "modeling"
    if any(k in fl for k in ("lattice", "tpms", "gyroid", "cell")):
        return "lattice"
    if any(k in fl for k in ("line", "arc", "curve", "polycurve", "plane", "point", "vector", "frame", "sphere", "cylinder", "box", "torus")):
        return "geometry"
    if any(k in fl for k in ("add<", "subtract<", "multiply<", "divide<", "sqrt<", "square<", "ramp", "field", "constant_")):
        return "math_and_fields"
    return "other"


def build_catalog_from_directory(
    directory: str | Path,
    save_path: Optional[str | Path] = None,
    include_custom_blocks: bool = False,
) -> Dict[str, Any]:
    """Recursively scan `.ntop` files in `directory` and extract verified nTop block signatures."""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {root}")

    catalog: Dict[str, Dict[str, Any]] = {}
    scanned_files = 0
    failed_files: List[Dict[str, str]] = []

    for ntop_file in sorted(root.rglob("*.ntop")):
        # Skip backup / history directories
        if ".ntop_history" in ntop_file.parts:
            continue
        try:
            nb = NTopNotebook.load(ntop_file)
            scanned_files += 1
        except Exception as exc:
            failed_files.append({"file": str(ntop_file), "error": str(exc)})
            continue

        bmap = {b["id"]: b for b in nb.fn.get("code", []) if "id" in b}
        lmap = {str(lv["id"]): lv for lv in nb.leaves if "id" in lv}

        for blk in nb.fn.get("code", []):
            func = blk.get("func", "")
            if not func:
                continue
            if not include_custom_blocks and func.startswith("user_func_"):
                continue

            entry = catalog.get(func)
            if entry is None:
                in_specs = []
                for idx, slot in enumerate(blk.get("inputs", [])):
                    cid = slot.get("instanceId", -1)
                    cb = bmap.get(cid, {})
                    spec: Dict[str, Any] = {
                        "index": idx,
                        "type": cb.get("type") or slot.get("type", "unknown"),
                    }
                    if slot.get("meta", {}).get("is_default"):
                        spec["is_default"] = True
                    if str(cid) in lmap:
                        lv = lmap[str(cid)].get("value")
                        if isinstance(lv, dict) and "base64" not in lv:
                            spec["sample_literal"] = lv
                    if slot.get("propchain"):
                        spec["sample_propchain"] = slot["propchain"]
                    in_specs.append(spec)

                catalog[func] = {
                    "func": func,
                    "category": _classify_func(func),
                    "output_type": blk.get("type", ""),
                    "input_count": len(blk.get("inputs", [])),
                    "inputs": in_specs,
                    "example_files": [str(ntop_file)],
                    "occurrences": 1,
                }
            else:
                entry["occurrences"] += 1
                fstr = str(ntop_file)
                if fstr not in entry["example_files"] and len(entry["example_files"]) < 5:
                    entry["example_files"].append(fstr)

    payload = {
        "source_directory": str(root),
        "scanned_files": scanned_files,
        "failed_files": failed_files,
        "block_count": len(catalog),
        "blocks": sorted(catalog.values(), key=lambda x: (x["category"], x["func"])),
    }

    dest = Path(save_path).expanduser().resolve() if save_path else DEFAULT_CATALOG_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "catalog_path": str(dest),
        "source_directory": str(root),
        "scanned_files": scanned_files,
        "failed_files_count": len(failed_files),
        "unique_blocks_indexed": len(catalog),
        "categories": sorted({b["category"] for b in payload["blocks"]}),
    }


def search_catalog(
    query: str = "",
    category: Optional[str] = None,
    catalog_path: Optional[str | Path] = None,
    limit: int = 25,
) -> Dict[str, Any]:
    """Search nTop block signatures across built-in essentials and any local scanned catalog."""
    merged: Dict[str, Dict[str, Any]] = {}
    for item in BUILTIN_BLOCKS:
        merged[item["func"]] = {**item, "source": "builtin"}

    cpath = Path(catalog_path).expanduser().resolve() if catalog_path else DEFAULT_CATALOG_PATH
    local_loaded = False
    if cpath.is_file():
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            for item in data.get("blocks", []):
                fn = item["func"]
                if fn in merged:
                    merged[fn]["example_files"] = item.get("example_files", [])
                    merged[fn]["occurrences"] = item.get("occurrences", 0)
                else:
                    merged[fn] = {**item, "source": "local_scan"}
            local_loaded = True
        except Exception:
            pass

    q = query.strip().lower()
    cat = category.strip().lower() if category else None

    matches: List[Dict[str, Any]] = []
    for entry in merged.values():
        if cat and entry.get("category", "").lower() != cat:
            continue
        if q:
            hay = f"{entry.get('func', '')} {entry.get('category', '')} {entry.get('output_type', '')} {entry.get('description', '')}".lower()
            if q not in hay:
                continue
        matches.append(entry)

    matches.sort(key=lambda x: (0 if x.get("source") == "builtin" else 1, -x.get("occurrences", 0), x["func"]))
    return {
        "query": query,
        "category": category,
        "local_catalog_loaded": local_loaded,
        "catalog_path": str(cpath) if local_loaded else None,
        "total_matches": len(matches),
        "returned": min(len(matches), limit),
        "blocks": matches[:limit],
    }
