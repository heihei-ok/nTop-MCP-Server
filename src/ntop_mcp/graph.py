"""High-level inspection, validation, and manipulation of nTop notebook block graphs."""

from __future__ import annotations

import copy
import json
import struct
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .container import MagicSection, NTopContainer, NTopFnSection, ObjContainer
from .units import parse_quantity


def _parse_color_rgba(color: Any) -> List[float]:
    """Parse hex '#RRGGBB' / '#RRGGBBAA' or 3/4-float list into [r, g, b, a] in 0..1."""
    if isinstance(color, str):
        s = color.strip().lstrip("#")
        if len(s) == 6:
            return [int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0, 1.0]
        if len(s) == 8:
            return [
                int(s[0:2], 16) / 255.0,
                int(s[2:4], 16) / 255.0,
                int(s[4:6], 16) / 255.0,
                int(s[6:8], 16) / 255.0,
            ]
        raise ValueError(f"Invalid hex color {color!r}; use '#RRGGBB' or '#RRGGBBAA'")
    if isinstance(color, (list, tuple)) and len(color) in (3, 4):
        vals = [float(c) for c in color]
        if any(v > 1.0 for v in vals):
            vals = [v / 255.0 for v in vals]
        if len(vals) == 3:
            vals.append(1.0)
        return vals
    raise ValueError(f"Invalid color {color!r}; pass '#RRGGBB' or [r, g, b, a]")


def _summarize_leaf_value(leaf: dict) -> Any:
    """Produce a concise, LLM-friendly summary of a leaf entry."""
    ltype = leaf.get("type", "")
    val = leaf.get("value")
    if not isinstance(val, dict):
        return val
    if "base64" in val:
        b64 = val["base64"]
        return f"<binary:{ltype}, base64_len={len(b64)}>"
    if "val" in val:
        out: Dict[str, Any] = {"val": val["val"]}
        if val.get("units"):
            out["units"] = val["units"]
        return out
    if "enum" in val:
        return {"enum": val["enum"]}
    if "value" in val and isinstance(val["value"], list):
        # vector / point
        comps = [c.get("val") if isinstance(c, dict) else c for c in val["value"]]
        out = {"components": comps}
        if val.get("units"):
            out["units"] = val["units"]
        return out
    return val


class NTopNotebook:
    """Structured editor and analyzer for an `.ntop` notebook."""

    def __init__(self, container: NTopContainer) -> None:
        self.container = container
        main_sec = container.get_section("main")
        if main_sec is None or main_sec.sec_type != "ntopfn":
            raise ValueError("Container does not have a valid 'main' ntopfn section")
        self._main_sec = main_sec
        self._main_fn_wrapper = NTopFnSection.from_payload(main_sec.payload)

        fn_sub = self._main_fn_wrapper.get_sub("fn")
        if fn_sub is None:
            raise ValueError("'main' section is missing 'fn' JSON sub-section")
        self._fn_sub = fn_sub
        self.fn: Dict[str, Any] = fn_sub.as_json()

        self._leaves_sub = self._main_fn_wrapper.get_sub("leaves")
        self._leaves_obj: Optional[ObjContainer] = None
        self._leaves_index_sub = None
        self.leaves: List[Dict[str, Any]] = []
        if self._leaves_sub is not None:
            self._leaves_obj = ObjContainer.from_payload(self._leaves_sub.payload)
            self._leaves_index_sub = self._leaves_obj.get_sub("index")
            if self._leaves_index_sub is not None:
                parsed = self._leaves_index_sub.as_json()
                if isinstance(parsed, list):
                    self.leaves = parsed

    @classmethod
    def load(cls, path: str | Path) -> "NTopNotebook":
        return cls(NTopContainer.load(path))

    def sync_to_container(self) -> None:
        """Serialize modified `fn` and `leaves` back into the underlying container."""
        self._fn_sub.set_json(self.fn)
        if self._leaves_sub is not None and self._leaves_obj is not None and self._leaves_index_sub is not None:
            self._leaves_index_sub.set_json(self.leaves)
            self._leaves_sub.payload = self._leaves_obj.to_payload()
        self._main_sec.payload = self._main_fn_wrapper.to_payload()

    def save(self, path: str | Path) -> Path:
        self.sync_to_container()
        return self.container.save(path)

    # ------------------------------------------------------------------
    # Indexing helpers
    # ------------------------------------------------------------------

    def _code_blocks(self) -> List[Dict[str, Any]]:
        return self.fn.get("code", [])

    def _block_map(self) -> Dict[int, Dict[str, Any]]:
        return {b["id"]: b for b in self._code_blocks() if "id" in b}

    def _leaf_map(self) -> Dict[str, Dict[str, Any]]:
        return {str(item.get("id")): item for item in self.leaves if "id" in item}

    def _get_version(self) -> Optional[List[int]]:
        turhe = self.container.get_section("turhe")
        if turhe is not None:
            try:
                return turhe.as_json().get("turver")
            except Exception:
                return None
        return None

    def _get_sections_meta(self) -> Optional[Dict[str, Any]]:
        sec = self.container.get_section("sections")
        if sec is not None:
            try:
                return sec.as_json()
            except Exception:
                return None
        return None

    def _root_group_block(self) -> Optional[Dict[str, Any]]:
        for b in self._code_blocks():
            if b.get("func") == "core.group<any>":
                return b
        return None

    def _section_membership(self) -> Dict[int, str]:
        """Map top-level block IDs to their notebook section name (using sections.poles)."""
        sec_meta = self._get_sections_meta()
        root = self._root_group_block()
        if not sec_meta or not root:
            return {}
        decorations = sec_meta.get("decorations", [])
        poles = sec_meta.get("poles", [])
        root_inputs = root.get("inputs", [])
        mapping: Dict[int, str] = {}
        for idx, dec in enumerate(decorations):
            if idx + 1 >= len(poles):
                break
            start_i, end_i = poles[idx], poles[idx + 1]
            sec_name = dec.get("name", f"Section {idx}")
            for slot in root_inputs[start_i:end_i]:
                inst_id = slot.get("instanceId")
                if inst_id is not None:
                    mapping[inst_id] = sec_name
        return mapping

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def inspect(self) -> Dict[str, Any]:
        """Return high-level metadata, sections, inputs, output, and block counts."""
        fn_def = self.fn.get("def", {})
        blocks = self._code_blocks()
        bmap = self._block_map()
        output_id = self.fn.get("output", -1)
        output_blk = bmap.get(output_id) if output_id != -1 else None

        var_blocks = [b for b in blocks if b.get("func", "").startswith("core.var")]
        leaf_blocks = [b for b in blocks if b.get("func", "") == ""]
        compute_blocks = [b for b in blocks if b.get("func", "") and not b.get("func", "").startswith("core.var")]

        # Sub-functions (sf0, sf1, ...)
        sub_funcs = []
        for s in self.container.sections:
            if s.sec_type == "ntopfn" and s.sec_name != "main":
                try:
                    sf_wrap = NTopFnSection.from_payload(s.payload)
                    sf_fn_sub = sf_wrap.get_sub("fn")
                    sf_fn = sf_fn_sub.as_json() if sf_fn_sub else {}
                    sf_def = sf_fn.get("def", {})
                    sub_funcs.append({
                        "section": s.sec_name,
                        "displayname": sf_def.get("displayname", ""),
                        "function": sf_def.get("function", ""),
                        "block_count": len(sf_fn.get("code", [])),
                        "input_count": len(sf_def.get("inputs", [])),
                    })
                except Exception:
                    sub_funcs.append({"section": s.sec_name})

        sec_meta = self._get_sections_meta()
        notebook_sections = []
        if sec_meta:
            decs = sec_meta.get("decorations", [])
            poles = sec_meta.get("poles", [])
            for i, d in enumerate(decs):
                count = (poles[i + 1] - poles[i]) if i + 1 < len(poles) else 0
                notebook_sections.append({
                    "name": d.get("name", ""),
                    "description": d.get("description", "").strip(),
                    "block_count": count,
                })

        return {
            "source_path": str(self.container.source_path) if self.container.source_path else None,
            "ntop_version": self._get_version(),
            "title": fn_def.get("displayname", ""),
            "description": fn_def.get("description", ""),
            "function_id": fn_def.get("function", ""),
            "model_inputs": [
                {
                    "index": idx,
                    "name": inp.get("name", ""),
                    "type": inp.get("type", ""),
                    "description": inp.get("description", ""),
                    "units": inp.get("units", {}),
                }
                for idx, inp in enumerate(fn_def.get("inputs", []))
            ],
            "output_block_id": output_id,
            "output_block": (
                {
                    "id": output_blk["id"],
                    "name": output_blk.get("name", ""),
                    "func": output_blk.get("func", ""),
                    "type": output_blk.get("type", ""),
                }
                if output_blk
                else None
            ),
            "output_def": fn_def.get("output", {}),
            "notebook_sections": notebook_sections,
            "counts": {
                "total_blocks": len(blocks),
                "variable_blocks": len(var_blocks),
                "compute_blocks": len(compute_blocks),
                "leaf_blocks": len(leaf_blocks),
                "leaf_values_stored": len(self.leaves),
                "sub_functions": len(sub_funcs),
                "container_sections": len(self.container.sections),
            },
            "sub_functions": sub_funcs,
            "container_sections": self.container.section_names(),
        }

    def list_variables(self) -> List[Dict[str, Any]]:
        """List all promoted variables (core.var<...>) and their resolved literal values."""
        bmap = self._block_map()
        lmap = self._leaf_map()
        sec_map = self._section_membership()
        results: List[Dict[str, Any]] = []

        for b in self._code_blocks():
            func = b.get("func", "")
            if not func.startswith("core.var"):
                continue
            inputs = b.get("inputs", [])
            first_inp = inputs[0] if inputs else {}
            child_id = first_inp.get("instanceId", -1)
            expr = first_inp.get("meta", {}).get("expression", "")
            child_blk = bmap.get(child_id)
            leaf = lmap.get(str(child_id))

            entry: Dict[str, Any] = {
                "id": b["id"],
                "name": b.get("name", ""),
                "type": b.get("type", ""),
                "func": func,
                "section": sec_map.get(b["id"], ""),
                "units": b.get("units", {}),
                "expression": expr,
                "source_block_id": child_id,
                "is_literal": bool(child_blk and child_blk.get("func", "") == ""),
            }
            if leaf is not None:
                entry["literal_value"] = _summarize_leaf_value(leaf)
            elif child_blk is not None:
                entry["source_func"] = child_blk.get("func", "")
                entry["source_name"] = child_blk.get("name", "")
            desc = (b.get("meta", {}) or {}).get("desc", "")
            if desc:
                entry["description"] = desc[:200]
            results.append(entry)

        return results

    def read_graph(
        self,
        filter_text: Optional[str] = None,
        section_name: Optional[str] = None,
        variables_only: bool = False,
        include_leaves: bool = False,
        limit: int = 150,
    ) -> Dict[str, Any]:
        """Return a structured view of the notebook block graph."""
        lmap = self._leaf_map()
        sec_map = self._section_membership()
        blocks = self._code_blocks()
        ft = filter_text.lower() if filter_text else None
        sn = section_name.lower() if section_name else None

        filtered: List[Dict[str, Any]] = []
        for b in blocks:
            func = b.get("func", "")
            is_leaf = func == ""
            is_var = func.startswith("core.var")
            if variables_only and not is_var:
                continue
            if not include_leaves and is_leaf:
                continue
            blk_sec = sec_map.get(b["id"], "")
            if sn and sn not in blk_sec.lower():
                continue
            if ft:
                haystack = f"{b.get('id')} {b.get('name', '')} {func} {b.get('type', '')} {blk_sec}".lower()
                if ft not in haystack:
                    continue

            inp_list = []
            for idx, slot in enumerate(b.get("inputs", [])):
                inst_id = slot.get("instanceId", -1)
                slot_info: Dict[str, Any] = {
                    "index": idx,
                    "instanceId": inst_id,
                }
                if slot.get("modelInputIdx", -1) != -1:
                    slot_info["modelInputIdx"] = slot["modelInputIdx"]
                meta = slot.get("meta", {}) or {}
                if meta.get("name"):
                    slot_info["name"] = meta["name"]
                if meta.get("expression"):
                    slot_info["expression"] = meta["expression"]
                if slot.get("propchain"):
                    slot_info["propchain"] = slot["propchain"]
                if str(inst_id) in lmap:
                    slot_info["literal"] = _summarize_leaf_value(lmap[str(inst_id)])
                inp_list.append(slot_info)

            item: Dict[str, Any] = {
                "id": b["id"],
                "name": b.get("name", ""),
                "func": func,
                "type": b.get("type", ""),
                "section": blk_sec,
                "inputs": inp_list,
            }
            if b.get("units"):
                item["units"] = b["units"]
            if str(b["id"]) in lmap:
                item["literal_value"] = _summarize_leaf_value(lmap[str(b["id"])])
            filtered.append(item)

        total_matched = len(filtered)
        truncated = total_matched > limit
        return {
            "total_blocks_in_notebook": len(blocks),
            "matched_count": total_matched,
            "returned_count": min(total_matched, limit),
            "truncated": truncated,
            "output_block_id": self.fn.get("output", -1),
            "blocks": filtered[:limit],
        }

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> Dict[str, Any]:
        """Validate block graph integrity and nTop Automate readiness."""
        blocks = self._code_blocks()
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        seen_ids: Dict[int, int] = {}
        for idx, b in enumerate(blocks):
            bid = b.get("id")
            if bid is None:
                errors.append({"code": "MISSING_ID", "message": f"Block at index {idx} has no id"})
                continue
            if bid in seen_ids:
                errors.append({"code": "DUPLICATE_ID", "block_id": bid, "message": f"Duplicate block ID {bid}"})
            seen_ids[bid] = idx

        bmap = self._block_map()
        lmap = self._leaf_map()
        model_inputs = self.fn.get("def", {}).get("inputs", [])

        output_id = self.fn.get("output", -1)
        if output_id == -1:
            warnings.append({
                "code": "OUTPUT_NOT_SET",
                "message": "Notebook output block is not set (output == -1). ntopcl -o / output_template.json requires an output block.",
            })
        elif output_id not in bmap:
            errors.append({
                "code": "INVALID_OUTPUT_ID",
                "block_id": output_id,
                "message": f"Output block ID {output_id} does not exist in code graph.",
            })

        # Check wiring & leaves
        referenced_ids: Set[int] = set()
        for b in blocks:
            bid = b.get("id")
            for slot_idx, slot in enumerate(b.get("inputs", [])):
                inst_id = slot.get("instanceId", -1)
                midx = slot.get("modelInputIdx", -1)
                if inst_id > 0:
                    referenced_ids.add(inst_id)
                    if inst_id not in bmap:
                        errors.append({
                            "code": "DANGLING_INPUT",
                            "block_id": bid,
                            "slot_index": slot_idx,
                            "target_id": inst_id,
                            "message": f"Block {bid} ({b.get('name','')}) input[{slot_idx}] references missing block ID {inst_id}",
                        })
                elif midx != -1:
                    if midx < 0 or midx >= len(model_inputs):
                        errors.append({
                            "code": "INVALID_MODEL_INPUT_IDX",
                            "block_id": bid,
                            "slot_index": slot_idx,
                            "model_input_idx": midx,
                            "message": f"Block {bid} input[{slot_idx}] references out-of-range modelInputIdx {midx}",
                        })

        # Reachability from root group and output
        roots: List[int] = []
        root_grp = self._root_group_block()
        if root_grp is not None:
            roots.append(root_grp["id"])
        if output_id != -1 and output_id in bmap:
            roots.append(output_id)

        reachable: Set[int] = set()
        stack = list(roots)
        while stack:
            cur = stack.pop()
            if cur in reachable or cur not in bmap:
                continue
            reachable.add(cur)
            for slot in bmap[cur].get("inputs", []):
                cid = slot.get("instanceId", -1)
                if cid != -1 and cid not in reachable:
                    stack.append(cid)

        unreachable = sorted(set(bmap.keys()) - reachable) if roots else []
        if unreachable:
            warnings.append({
                "code": "UNREACHABLE_BLOCKS",
                "count": len(unreachable),
                "sample_ids": unreachable[:15],
                "message": f"{len(unreachable)} block(s) are unreachable from the notebook root/output.",
            })

        # Orphan leaf entries
        orphan_leaves = [lid for lid in lmap if lid.isdigit() and int(lid) not in bmap]
        if orphan_leaves:
            warnings.append({
                "code": "ORPHAN_LEAVES",
                "count": len(orphan_leaves),
                "sample_ids": orphan_leaves[:15],
                "message": f"{len(orphan_leaves)} stored leaf value(s) have no corresponding block in code.",
            })

        return {
            "valid": len(errors) == 0,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "automate_ready": len(errors) == 0 and output_id != -1,
            "errors": errors,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def _resolve_block(self, target: int | str) -> Dict[str, Any]:
        bmap = self._block_map()
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            bid = int(target)
            if bid not in bmap:
                raise KeyError(f"Block ID {bid} not found")
            return bmap[bid]
        # Match by exact name first, then case-insensitive
        t_str = str(target)
        exact = [b for b in self._code_blocks() if b.get("name") == t_str]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            ids = [b["id"] for b in exact]
            raise ValueError(f"Multiple blocks named {t_str!r}: IDs {ids}. Specify block ID instead.")
        ci = [b for b in self._code_blocks() if b.get("name", "").lower() == t_str.lower()]
        if len(ci) == 1:
            return ci[0]
        raise KeyError(f"Block {target!r} not found")

    def set_literal(
        self,
        target: int | str,
        value: Any,
        expression: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a literal leaf value by leaf block ID, variable block ID, or variable name."""
        blk = self._resolve_block(target)
        bmap = self._block_map()
        parent_var: Optional[Dict[str, Any]] = None

        # If target is a core.var<...> block, follow its first input to the underlying leaf
        if blk.get("func", "").startswith("core.var"):
            inputs = blk.get("inputs", [])
            if not inputs:
                raise ValueError(f"Variable block {blk['id']} ({blk.get('name')}) has no input slot")
            child_id = inputs[0].get("instanceId", -1)
            child = bmap.get(child_id)
            if child is None or child.get("func", "") != "":
                raise ValueError(
                    f"Variable block {blk['id']} ({blk.get('name')}) is computed from block {child_id} "
                    f"({child.get('func') if child else 'missing'}), not a literal leaf."
                )
            parent_var = blk
            blk = child
        elif blk.get("func", "") != "":
            raise ValueError(
                f"Block {blk['id']} ({blk.get('name')}) is a compute block ({blk.get('func')}), not a literal."
            )

        leaf_id_str = str(blk["id"])
        lmap = self._leaf_map()
        leaf = lmap.get(leaf_id_str)
        if leaf is None:
            raise KeyError(f"Leaf block {leaf_id_str} has no stored entry in leaves index")

        ltype = leaf.get("type", "")
        old_summary = _summarize_leaf_value(leaf)
        lval = leaf.get("value")

        if isinstance(lval, dict) and "enum" in lval:
            lval["enum"] = int(value)
        elif isinstance(lval, dict) and "value" in lval and isinstance(lval["value"], list):
            # Vector / Point
            if not isinstance(value, (list, tuple)) or len(value) != len(lval["value"]):
                raise ValueError(
                    f"Leaf {leaf_id_str} ({ltype}) expects a list of {len(lval['value'])} numbers"
                )
            for item, num in zip(lval["value"], value):
                if isinstance(item, dict):
                    item["val"] = float(num)
                    item["isFinite"] = True
        elif isinstance(lval, dict) and "val" in lval:
            if ltype == "bool":
                if isinstance(value, str):
                    lval["val"] = value.strip().lower() in ("true", "1", "yes")
                else:
                    lval["val"] = bool(value)
            elif ltype == "integer":
                lval["val"] = int(value)
            elif ltype == "real":
                si_val, parsed_units, parsed_expr = parse_quantity(
                    value, fallback_units=lval.get("units") or {}
                )
                lval["val"] = si_val
                if parsed_units:
                    lval["units"] = parsed_units
                    blk["units"] = dict(parsed_units)
                if "isFinite" in lval:
                    lval["isFinite"] = True
                if expression is None and parsed_expr is not None:
                    expression = parsed_expr
            else:
                lval["val"] = value
        else:
            leaf["value"] = value

        # Update or clear stale display expression on parent var slot
        if parent_var is not None and parent_var.get("inputs"):
            meta = parent_var["inputs"][0].setdefault("meta", {})
            if expression is not None:
                meta["expression"] = expression
            elif meta.get("expression"):
                # Clear stale expression (e.g. "1000mm") so GUI/CLI doesn't show outdated text
                meta["expression"] = ""

        return {
            "leaf_block_id": blk["id"],
            "variable_block_id": parent_var["id"] if parent_var else None,
            "variable_name": parent_var.get("name") if parent_var else blk.get("name"),
            "leaf_type": ltype,
            "old_value": old_summary,
            "new_value": _summarize_leaf_value(leaf),
            "expression": (
                parent_var["inputs"][0].get("meta", {}).get("expression", "")
                if parent_var and parent_var.get("inputs")
                else None
            ),
        }

    def set_output(self, target: int | str) -> Dict[str, Any]:
        """Set the notebook's output block (required for `ntopcl -o` and output templates)."""
        blk = self._resolve_block(target)
        old_output = self.fn.get("output", -1)
        self.fn["output"] = blk["id"]

        blk_type = blk.get("type", "")
        fn_def = self.fn.setdefault("def", {})
        if blk_type:
            fn_def.setdefault("output", {})["type"] = blk_type
            fn_def.setdefault("newSystemOutput", {})["type"] = blk_type

        return {
            "old_output_block_id": old_output,
            "new_output_block_id": blk["id"],
            "block_name": blk.get("name", ""),
            "block_func": blk.get("func", ""),
            "block_type": blk_type,
        }

    def rewire_input(
        self,
        block_target: int | str,
        input_index: int,
        new_instance_id: Optional[int] = None,
        expression: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Rewire an input slot of a block to another block ID and/or update its expression."""
        blk = self._resolve_block(block_target)
        inputs = blk.get("inputs", [])
        if input_index < 0 or input_index >= len(inputs):
            raise IndexError(
                f"Block {blk['id']} ({blk.get('name')}) has {len(inputs)} inputs; index {input_index} out of range"
            )
        slot = inputs[input_index]
        old_inst = slot.get("instanceId", -1)
        old_expr = (slot.get("meta") or {}).get("expression", "")

        if new_instance_id is not None:
            bmap = self._block_map()
            if new_instance_id != -1 and new_instance_id not in bmap:
                raise KeyError(f"Target instanceId {new_instance_id} does not exist in notebook")
            slot["instanceId"] = new_instance_id

        if expression is not None:
            slot.setdefault("meta", {})["expression"] = expression

        return {
            "block_id": blk["id"],
            "block_name": blk.get("name", ""),
            "input_index": input_index,
            "old_instance_id": old_inst,
            "new_instance_id": slot.get("instanceId", -1),
            "old_expression": old_expr,
            "new_expression": (slot.get("meta") or {}).get("expression", ""),
        }

    def prune_to_output(self, output_target: Optional[int | str] = None) -> Dict[str, Any]:
        """Prune unreachable blocks not required by the output block (and keep root group clean)."""
        if output_target is not None:
            self.set_output(output_target)

        output_id = self.fn.get("output", -1)
        if output_id == -1:
            raise ValueError("Cannot prune: output block is not set. Provide output_target first.")

        bmap = self._block_map()
        if output_id not in bmap:
            raise KeyError(f"Output block ID {output_id} not found")

        keep: Set[int] = set()
        stack = [output_id]
        while stack:
            cur = stack.pop()
            if cur in keep or cur not in bmap:
                continue
            keep.add(cur)
            for slot in bmap[cur].get("inputs", []):
                cid = slot.get("instanceId", -1)
                if cid != -1 and cid not in keep:
                    stack.append(cid)

        # Keep the root group block if present, filtering its inputs to kept blocks
        root_grp = self._root_group_block()
        if root_grp is not None:
            keep.add(root_grp["id"])
            root_grp["inputs"] = [
                slot for slot in root_grp.get("inputs", []) if slot.get("instanceId") in keep
            ]

        orig_blocks = len(self._code_blocks())
        orig_leaves = len(self.leaves)
        self.fn["code"] = [b for b in self._code_blocks() if b.get("id") in keep]
        self.leaves = [
            lv for lv in self.leaves if not str(lv.get("id", "")).isdigit() or int(lv["id"]) in keep
        ]

        return {
            "output_block_id": output_id,
            "original_blocks": orig_blocks,
            "remaining_blocks": len(self.fn["code"]),
            "removed_blocks": orig_blocks - len(self.fn["code"]),
            "original_leaves": orig_leaves,
            "remaining_leaves": len(self.leaves),
            "removed_leaves": orig_leaves - len(self.leaves),
        }

    # ------------------------------------------------------------------
    # Graph Construction (Add Literals, Blocks, Variables, Sections)
    # ------------------------------------------------------------------

    def _next_block_id(self) -> int:
        ids = [b["id"] for b in self._code_blocks() if isinstance(b.get("id"), int)]
        leaf_ids = [int(lv["id"]) for lv in self.leaves if str(lv.get("id", "")).isdigit()]
        return max(ids + leaf_ids + [100]) + 1

    def _insert_into_section(self, block_id: int, section_name: Optional[str] = None) -> str:
        """Insert a top-level block ID into `core.group<any>` and update `sections.poles`."""
        root = self._root_group_block()
        if root is None:
            raise ValueError("Notebook has no root core.group<any> block")

        new_slot = {
            "instanceId": block_id,
            "meta": {"expression": "", "is_default": False, "name": ""},
            "modelInputIdx": -1,
            "propchain": [],
        }

        sec_section = self.container.get_section("sections")
        if sec_section is None:
            root.setdefault("inputs", []).append(new_slot)
            return ""

        sec_meta = sec_section.as_json()
        decs = sec_meta.get("decorations", [])
        poles = sec_meta.get("poles", [])

        if not decs or len(poles) != len(decs) + 1:
            root.setdefault("inputs", []).append(new_slot)
            return ""

        target_idx = len(decs) - 1
        if section_name is not None:
            matches = [i for i, d in enumerate(decs) if d.get("name", "").lower() == section_name.lower()]
            if not matches:
                # Auto-create the requested section at the end
                decs.append({"collapse": False, "description": "", "name": section_name})
                poles.append(poles[-1])
                target_idx = len(decs) - 1
            else:
                target_idx = matches[0]

        insert_pos = poles[target_idx + 1]
        root.setdefault("inputs", []).insert(insert_pos, new_slot)
        for j in range(target_idx + 1, len(poles)):
            poles[j] += 1

        sec_section.set_json(sec_meta)
        return decs[target_idx].get("name", "")

    def add_section(self, name: str, description: str = "", collapse: bool = False) -> Dict[str, Any]:
        """Append a new named section to the notebook's `sections` metadata."""
        sec_section = self.container.get_section("sections")
        if sec_section is None:
            raise ValueError("Notebook does not contain a 'sections' metadata section")
        sec_meta = sec_section.as_json()
        decs = sec_meta.setdefault("decorations", [])
        poles = sec_meta.setdefault("poles", [0])
        for d in decs:
            if d.get("name", "").lower() == name.lower():
                return {"created": False, "name": d["name"], "message": "Section already exists"}
        decs.append({"collapse": collapse, "description": description, "name": name})
        poles.append(poles[-1] if poles else 0)
        sec_section.set_json(sec_meta)
        return {"created": True, "name": name, "section_index": len(decs) - 1}

    def add_literal(
        self,
        leaf_type: str,
        value: Any,
        name: Optional[str] = None,
        units: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Add a new anonymous literal leaf node (`func == ""`) and store its value in `leaves`.

        Supports human-friendly unit strings for `real` literals such as `"10mm"`, `"45deg"`,
        or `{"value": 10, "unit": "mm"}`.
        """
        bid = self._next_block_id()
        units_dict = dict(units) if units is not None else {}
        parsed_expr: Optional[str] = None

        if leaf_type == "real":
            si_val, parsed_units, parsed_expr = parse_quantity(value, fallback_units=units_dict)
            units_dict = parsed_units
            leaf_val: Any = {"isFinite": True, "units": units_dict, "val": si_val}
        elif leaf_type == "integer":
            leaf_val = {"val": int(value)}
        elif leaf_type == "bool":
            if isinstance(value, str):
                leaf_val = {"val": value.strip().lower() in ("true", "1", "yes")}
            else:
                leaf_val = {"val": bool(value)}
        elif leaf_type == "unit_length_enum":
            leaf_val = value if isinstance(value, dict) else {"id": str(value)}
        elif leaf_type.endswith("_enum"):
            leaf_val = value if isinstance(value, dict) else {"enum": int(value)}
        elif leaf_type in ("file_path", "text", "string"):
            leaf_val = value if isinstance(value, dict) else {"val": str(value)}
        elif leaf_type in ("vector", "point"):
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                raise ValueError(f"{leaf_type} requires a 3-element numeric list")
            comps = []
            for c in value:
                si_c, u_c, _ = parse_quantity(c, fallback_units=units_dict)
                if u_c and not units_dict:
                    units_dict = u_c
                comps.append({"isFinite": True, "val": si_c})
            leaf_val = {"units": units_dict, "value": comps}
        else:
            leaf_val = value if isinstance(value, dict) else {"val": value}

        blk_name = name or f"{leaf_type}_anon_{bid}"
        block = {
            "autoc": True,
            "bldst": 0,
            "func": "",
            "id": bid,
            "inputs": [],
            "meta": {"desc": "", "import": 0},
            "name": blk_name,
            "optional": False,
            "paust": 0,
            "qual": 0,
            "type": leaf_type,
            "units": units_dict,
            "unitsKnown": True,
        }
        self.fn.setdefault("code", []).append(block)
        self.leaves.append({"id": str(bid), "type": leaf_type, "value": leaf_val})
        out: Dict[str, Any] = {"block_id": bid, "name": blk_name, "type": leaf_type, "value": leaf_val}
        if parsed_expr is not None:
            out["expression"] = parsed_expr
        return out

    def add_block(
        self,
        func: str,
        block_type: str,
        inputs: List[Dict[str, Any]],
        name: Optional[str] = None,
        units: Optional[Dict[str, int]] = None,
        section_name: Optional[str] = None,
        add_to_root: bool = False,
    ) -> Dict[str, Any]:
        """Add a compute block to `main.fn.code` and optionally place it in a notebook section.

        Each item in `inputs` may be a dict with keys:
        - `instanceId` (int, default -1)
        - `modelInputIdx` (int, default -1)
        - `propchain` (list[str], default [])
        - `expression` (str, default "")
        - `name` (str, default "")
        - `is_default` (bool, default False)
        """
        bid = self._next_block_id()
        bmap = self._block_map()
        normalized_inputs: List[Dict[str, Any]] = []
        for slot in inputs:
            inst_id = int(slot.get("instanceId", -1))
            if inst_id > 0 and inst_id not in bmap:
                raise KeyError(f"Input references non-existent block ID {inst_id}")
            norm_slot: Dict[str, Any] = {
                "instanceId": inst_id,
                "meta": {
                    "expression": slot.get("expression", ""),
                    "is_default": bool(slot.get("is_default", False)),
                    "name": slot.get("name", ""),
                },
                "modelInputIdx": int(slot.get("modelInputIdx", -1)),
                "propchain": list(slot.get("propchain", [])),
            }
            if "type" in slot and slot["type"]:
                norm_slot["type"] = slot["type"]
            normalized_inputs.append(norm_slot)

        blk_name = name or f"Block_{bid}"
        block = {
            "autoc": False,
            "bldst": 0,
            "func": func,
            "id": bid,
            "inputs": normalized_inputs,
            "meta": {"desc": "", "import": 0},
            "name": blk_name,
            "optional": False,
            "paust": 0,
            "qual": 0,
            "type": block_type,
            "units": dict(units) if units else {},
            "unitsKnown": True,
        }
        self.fn.setdefault("code", []).append(block)

        placed_section = ""
        if add_to_root or section_name is not None:
            placed_section = self._insert_into_section(bid, section_name=section_name)

        return {
            "block_id": bid,
            "name": blk_name,
            "func": func,
            "type": block_type,
            "section": placed_section,
        }

    def add_variable(
        self,
        name: str,
        var_type: str,
        source_block_id: int,
        section_name: Optional[str] = None,
        units: Optional[Dict[str, int]] = None,
        expression: str = "",
    ) -> Dict[str, Any]:
        """Add a top-level promoted variable (`core.var<var_type>`) into a notebook section."""
        bmap = self._block_map()
        if source_block_id not in bmap:
            raise KeyError(f"Source block ID {source_block_id} not found")
        src_blk = bmap[source_block_id]
        return self.add_block(
            func=f"core.var<{var_type}>",
            block_type=var_type,
            inputs=[
                {
                    "instanceId": source_block_id,
                    "name": src_blk.get("name", ""),
                    "expression": expression,
                    "is_default": False,
                }
            ],
            name=name,
            units=units if units is not None else src_blk.get("units", {}),
            section_name=section_name,
            add_to_root=True,
        )

    # ------------------------------------------------------------------
    # Viewport Visibility, Input Promotion & Transactional Batch Editing
    # ------------------------------------------------------------------

    def set_visibility(
        self,
        targets: List[int | str],
        visible: bool = True,
        solo: bool = False,
        color: Optional[Any] = None,
        transparent: Optional[bool] = None,
        opacity: Optional[float] = None,
        wireframe: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update block viewport visibility, color, and opacity in the `.ntop` `view` section."""
        view_sec = self.container.get_section("view")
        if view_sec is None:
            raise ValueError("Notebook does not have a 'view' section")

        view_list = view_sec.as_json()
        if not isinstance(view_list, list):
            raise ValueError("Unexpected 'view' section format (expected JSON list)")

        resolved_ids = [self._resolve_block(t)["id"] for t in targets]
        target_set = set(resolved_ids)
        rgba = _parse_color_rgba(color) if color is not None else None

        by_id = {entry.get("id"): entry for entry in view_list if isinstance(entry, dict) and "id" in entry}
        hidden_count = 0

        if solo:
            for entry in view_list:
                if isinstance(entry, dict) and entry.get("id") not in target_set:
                    if entry.get("visible"):
                        hidden_count += 1
                    entry["visible"] = False

        for bid in resolved_ids:
            entry = by_id.get(bid)
            if entry is None:
                entry = {
                    "attributes": [
                        {"id": 11, "name": "display_mode", "type": 2, "value": 0},
                        {"id": 13, "name": "color", "type": 5, "value": rgba or [0.724, 0.724, 0.724, 1.0]},
                        {"id": 14, "name": "transparency", "type": 1, "value": 0},
                        {"id": 15, "name": "opacity", "type": 3, "value": 50},
                    ],
                    "id": bid,
                    "modelId": -1,
                    "opacity": 0.5,
                    "transparent": False,
                    "visible": bool(visible),
                    "wireframe": False,
                }
                view_list.append(entry)
                by_id[bid] = entry
            else:
                entry["visible"] = bool(visible)

            attrs = entry.setdefault("attributes", [])
            attr_by_name = {a.get("name"): a for a in attrs if isinstance(a, dict)}

            if rgba is not None:
                if "color" in attr_by_name:
                    attr_by_name["color"]["value"] = rgba
                else:
                    attrs.append({"id": 13, "name": "color", "type": 5, "value": rgba})
            if transparent is not None:
                entry["transparent"] = bool(transparent)
                if "transparency" in attr_by_name:
                    attr_by_name["transparency"]["value"] = 1 if transparent else 0
            if opacity is not None:
                op_clamped = max(0.0, min(1.0, float(opacity)))
                entry["opacity"] = op_clamped
                if "opacity" in attr_by_name:
                    attr_by_name["opacity"]["value"] = int(round(op_clamped * 100))
            if wireframe is not None:
                entry["wireframe"] = bool(wireframe)

        view_sec.set_json(view_list)
        return {
            "updated_block_ids": resolved_ids,
            "visible": bool(visible),
            "solo": bool(solo),
            "hidden_other_blocks": hidden_count,
            "color": rgba,
        }

    def promote_to_input(
        self,
        target: int | str,
        input_name: Optional[str] = None,
        description: str = "",
    ) -> Dict[str, Any]:
        """Promote an existing variable (`core.var<...>`) into a Notebook Input (`def.inputs`).

        This allows passing values via `ntopcl -j input.json` and `run_parameter_sweep`
        while preserving all downstream block connections.
        """
        blk = self._resolve_block(target)
        if not blk.get("func", "").startswith("core.var"):
            raise ValueError(
                f"Block {blk['id']} ({blk.get('name')}) is not a promoted variable (core.var<...>)"
            )
        inputs = blk.get("inputs", [])
        if not inputs:
            raise ValueError(f"Variable block {blk['id']} has no input slot")

        slot = inputs[0]
        if slot.get("modelInputIdx", -1) != -1:
            return {
                "already_promoted": True,
                "model_input_idx": slot["modelInputIdx"],
                "variable_block_id": blk["id"],
                "name": blk.get("name", ""),
            }

        var_type = blk.get("type", "real")
        units = dict(blk.get("units") or {})
        fn_def = self.fn.setdefault("def", {})
        model_inputs = fn_def.setdefault("inputs", [])
        new_idx = len(model_inputs)
        in_name = input_name or blk.get("name") or f"Input_{new_idx}"

        inp_def: Dict[str, Any] = {
            "cardinality": "one",
            "description": description,
            "name": in_name,
            "parameterization": 0,
            "type": {"default_type": var_type, "type": var_type},
            "unitsMult": [0, 1],
        }
        if units:
            inp_def["unitsReq"] = units
        model_inputs.append(inp_def)

        # Rewire variable's source slot to read from modelInputIdx
        old_inst = slot.get("instanceId", -1)
        old_leaf = self._leaf_map().get(str(old_inst))
        slot["instanceId"] = -1
        slot["modelInputIdx"] = new_idx
        slot.setdefault("meta", {})["expression"] = ""

        # Create the required companion `inp{new_idx}` ntopfn section holding the input's default value
        inp_leaf_id = 102
        inp_fn_json: Dict[str, Any] = {
            "code": [
                {
                    "autoc": False,
                    "bldst": 0,
                    "func": "core.group<any>",
                    "id": 100,
                    "inputs": [
                        {
                            "instanceId": 101,
                            "meta": {"expression": "", "is_default": False, "name": ""},
                            "modelInputIdx": -1,
                            "propchain": [],
                        }
                    ],
                    "meta": {"desc": "", "import": 0},
                    "name": "root",
                    "optional": False,
                    "paust": 0,
                    "qual": 2,
                    "type": "group",
                    "units": {},
                    "unitsKnown": True,
                },
                {
                    "autoc": False,
                    "bldst": 0,
                    "func": f"core.var<{var_type}>",
                    "id": 101,
                    "inputs": [
                        {
                            "instanceId": inp_leaf_id,
                            "meta": {"expression": "", "is_default": False, "name": ""},
                            "modelInputIdx": -1,
                            "propchain": [],
                        }
                    ],
                    "meta": {"desc": "", "import": 0},
                    "name": in_name,
                    "optional": False,
                    "paust": 0,
                    "qual": 0,
                    "type": var_type,
                    "units": units,
                    "unitsKnown": True,
                },
                {
                    "autoc": True,
                    "bldst": 0,
                    "func": "",
                    "id": inp_leaf_id,
                    "inputs": [],
                    "meta": {"desc": "", "import": 0},
                    "name": var_type,
                    "optional": False,
                    "paust": 0,
                    "qual": 0,
                    "type": var_type,
                    "units": units,
                    "unitsKnown": True,
                },
            ],
            "def": {
                "description": description,
                "displayname": in_name,
                "function": f"user_func_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:12]}",
                "inputs": [],
                "newSystemOutput": {"type": var_type},
                "output": {"default_type": var_type, "type": var_type},
                "outputBaseUnits": {},
                "releasestage": 2,
                "runaspects.defaultVariadicInputs": 1,
                "runaspects.defaultname": "",
                "runaspects.disablelistprocessing": False,
                "runaspects.embeddable": False,
                "runaspects.implicitparam": 0,
                "runaspects.iterative": False,
                "runaspects.overloadpriority": 0,
                "runaspects.progstages": -1,
                "runaspects.rons": 1,
                "runaspects.searchable": True,
                "runaspects.security": False,
                "runaspects.sideeffects": False,
                "runaspects.startvisible": True,
                "unitsVariadicMultiplier": [0, 1],
            },
            "dependencies": [],
            "externalDependencies": [],
            "fninf": None,
            "output": 101,
        }

        if old_leaf is not None:
            default_leaf = copy.deepcopy(old_leaf)
            default_leaf["id"] = str(inp_leaf_id)
            inp_leaves = [default_leaf]
        else:
            inp_leaves = []

        fn_sub = MagicSection(
            "json",
            "fn",
            json.dumps(inp_fn_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        idx_sub = MagicSection(
            "json",
            "index",
            json.dumps(inp_leaves, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        leaves_obj = ObjContainer(prefix_bytes=struct.pack("<Q", 8), sub_sections=[idx_sub])
        leaves_sub = MagicSection("obj_container", "leaves", leaves_obj.to_payload())
        inp_sec = MagicSection(
            "ntopfn",
            f"inp{new_idx}",
            NTopFnSection(sub_sections=[fn_sub, leaves_sub]).to_payload(),
        )

        # Insert right after `main` and any existing `inp*` sections
        insert_idx = len(self.container.sections)
        for i, s in enumerate(self.container.sections):
            if s.sec_name == "main" or s.sec_name.startswith("inp"):
                insert_idx = i + 1
        self.container.sections.insert(insert_idx, inp_sec)

        return {
            "already_promoted": False,
            "model_input_idx": new_idx,
            "input_section": f"inp{new_idx}",
            "input_name": in_name,
            "variable_block_id": blk["id"],
            "previous_source_id": old_inst,
            "type": var_type,
            "units": units,
        }

    def batch_edit(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply a list of declarative graph operations atomically in memory with `$alias` references.

        Supported `op` values:
        - `add_section`: `{op, name, description?, collapse?}`
        - `add_literal`: `{op, as?, leaf_type, value, name?, units?}`
        - `add_block`: `{op, as?, func, block_type, inputs, name?, units?, section_name?, add_to_root?}`
          (each element of `inputs` may be an int ID, a `"$alias"` string, or a full slot dict)
        - `add_variable`: `{op, as?, name, var_type, source_block_id, section_name?, units?, expression?}`
        - `set_literal`: `{op, target, value, expression?}`
        - `rewire_input`: `{op, block_target, input_index, new_instance_id?, expression?}`
        - `set_output`: `{op, target}`
        - `set_visibility`: `{op, targets, visible?, solo?, color?, transparent?, opacity?, wireframe?}`
        - `promote_to_input`: `{op, target, input_name?, description?}`
        """
        aliases: Dict[str, int] = {}
        results: List[Dict[str, Any]] = []

        def _resolve_ref(val: Any) -> Any:
            if isinstance(val, str) and val.startswith("$"):
                if val not in aliases:
                    raise KeyError(f"Unknown batch alias {val!r}. Defined so far: {sorted(aliases.keys())}")
                return aliases[val]
            return val

        def _normalize_block_inputs(raw_inputs: List[Any]) -> List[Dict[str, Any]]:
            norm: List[Dict[str, Any]] = []
            for item in raw_inputs:
                if isinstance(item, int):
                    norm.append({"instanceId": item})
                elif isinstance(item, str) and item.startswith("$"):
                    norm.append({"instanceId": _resolve_ref(item)})
                elif isinstance(item, dict):
                    copy_d = dict(item)
                    if "instanceId" in copy_d:
                        copy_d["instanceId"] = _resolve_ref(copy_d["instanceId"])
                    norm.append(copy_d)
                else:
                    raise ValueError(f"Unsupported input slot specification: {item!r}")
            return norm

        for step_idx, spec in enumerate(operations):
            op = spec.get("op")
            alias = spec.get("as")
            if not op:
                raise ValueError(f"Operation #{step_idx} is missing required 'op' key")

            if op == "add_section":
                res = self.add_section(
                    name=spec["name"],
                    description=spec.get("description", ""),
                    collapse=bool(spec.get("collapse", False)),
                )
            elif op == "add_literal":
                res = self.add_literal(
                    leaf_type=spec["leaf_type"],
                    value=spec["value"],
                    name=spec.get("name"),
                    units=spec.get("units"),
                )
            elif op == "add_block":
                res = self.add_block(
                    func=spec["func"],
                    block_type=spec["block_type"],
                    inputs=_normalize_block_inputs(spec.get("inputs", [])),
                    name=spec.get("name"),
                    units=spec.get("units"),
                    section_name=spec.get("section_name"),
                    add_to_root=bool(spec.get("add_to_root", False)),
                )
            elif op == "add_variable":
                res = self.add_variable(
                    name=spec["name"],
                    var_type=spec["var_type"],
                    source_block_id=int(_resolve_ref(spec["source_block_id"])),
                    section_name=spec.get("section_name"),
                    units=spec.get("units"),
                    expression=spec.get("expression", ""),
                )
            elif op == "set_literal":
                res = self.set_literal(
                    target=_resolve_ref(spec["target"]),
                    value=spec["value"],
                    expression=spec.get("expression"),
                )
            elif op == "rewire_input":
                new_id = spec.get("new_instance_id")
                res = self.rewire_input(
                    block_target=_resolve_ref(spec["block_target"]),
                    input_index=int(spec["input_index"]),
                    new_instance_id=int(_resolve_ref(new_id)) if new_id is not None else None,
                    expression=spec.get("expression"),
                )
            elif op == "set_output":
                res = self.set_output(_resolve_ref(spec["target"]))
            elif op == "set_visibility":
                tlist = [_resolve_ref(t) for t in spec.get("targets", [])]
                res = self.set_visibility(
                    targets=tlist,
                    visible=bool(spec.get("visible", True)),
                    solo=bool(spec.get("solo", False)),
                    color=spec.get("color"),
                    transparent=spec.get("transparent"),
                    opacity=spec.get("opacity"),
                    wireframe=spec.get("wireframe"),
                )
            elif op == "promote_to_input":
                res = self.promote_to_input(
                    target=_resolve_ref(spec["target"]),
                    input_name=spec.get("input_name"),
                    description=spec.get("description", ""),
                )
            else:
                raise ValueError(f"Unknown batch op {op!r} at step #{step_idx}")

            if alias:
                if not isinstance(alias, str) or not alias.startswith("$"):
                    raise ValueError(f"Alias {alias!r} must be a string starting with '$'")
                bid = res.get("block_id")
                if bid is None:
                    raise ValueError(f"Operation {op!r} at step #{step_idx} did not produce a block_id for alias {alias!r}")
                aliases[alias] = int(bid)

            results.append({"step": step_idx, "op": op, "alias": alias, "result": res})

        validation = self.validate()
        return {
            "operation_count": len(operations),
            "aliases": aliases,
            "results": results,
            "validation": validation,
        }

    def package_as_custom_block(
        self,
        output_target: int | str,
        block_name: str,
        input_targets: Optional[List[int | str]] = None,
        description: str = "",
        prune: bool = True,
        function_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Package this notebook (or a pruned branch) as a reusable nTop Custom Block `.ntop`.

        Preserving a stable `function_id` across updates allows nTop GUI's
        `File -> Import -> Custom Block` workflow to detect the updated version and
        hot-update the block inside an already-open parent notebook without restarting GUI.
        """
        promoted: List[Dict[str, Any]] = []
        for t in input_targets or []:
            promoted.append(self.promote_to_input(t))

        out_info = self.set_output(output_target)
        prune_stats = self.prune_to_output() if prune else None

        fn_def = self.fn.setdefault("def", {})
        fn_def["displayname"] = block_name
        if description:
            fn_def["description"] = description
        if function_id:
            fn_def["function"] = function_id
        elif not fn_def.get("function"):
            fn_def["function"] = (
                f"user_func_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:4]}_"
                f"{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:4]}_{uuid.uuid4().hex[:12]}"
            )

        return {
            "block_name": block_name,
            "function_id": fn_def["function"],
            "output": out_info,
            "promoted_inputs": promoted,
            "total_inputs": len(fn_def.get("inputs", [])),
            "prune_stats": prune_stats,
            "validation": self.validate(),
        }
