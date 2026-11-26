"""Human-friendly unit parsing and SI conversion for nTop literal values.

Inside `.ntop` files, `real` literals store values in SI base units (meters, radians, kg, Pa, etc.)
together with an integer exponent `units` dictionary such as `{"length": 1}`.
This module converts strings like `"10mm"`, `"2.5 cm"`, `"45deg"`, or dicts like
`{"value": 10, "unit": "mm"}` into `(si_value, units_dict, display_expression)`.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional, Tuple


# unit_key -> (multiplier_to_si, ntop_units_dict, canonical_suffix)
_UNIT_TABLE: Dict[str, Tuple[float, Dict[str, int], str]] = {
    # Length -> meters
    "nm": (1e-9, {"length": 1}, "nm"),
    "um": (1e-6, {"length": 1}, "um"),
    "μm": (1e-6, {"length": 1}, "um"),
    "mm": (1e-3, {"length": 1}, "mm"),
    "cm": (1e-2, {"length": 1}, "cm"),
    "m": (1.0, {"length": 1}, "m"),
    "in": (0.0254, {"length": 1}, "in"),
    "inch": (0.0254, {"length": 1}, "in"),
    "ft": (0.3048, {"length": 1}, "ft"),
    # Area -> m^2
    "mm2": (1e-6, {"length": 2}, "mm^2"),
    "mm^2": (1e-6, {"length": 2}, "mm^2"),
    "cm2": (1e-4, {"length": 2}, "cm^2"),
    "cm^2": (1e-4, {"length": 2}, "cm^2"),
    "m2": (1.0, {"length": 2}, "m^2"),
    "m^2": (1.0, {"length": 2}, "m^2"),
    # Volume -> m^3
    "mm3": (1e-9, {"length": 3}, "mm^3"),
    "mm^3": (1e-9, {"length": 3}, "mm^3"),
    "cm3": (1e-6, {"length": 3}, "cm^3"),
    "cm^3": (1e-6, {"length": 3}, "cm^3"),
    "m3": (1.0, {"length": 3}, "m^3"),
    "m^3": (1.0, {"length": 3}, "m^3"),
    # Angle -> radians
    "deg": (math.pi / 180.0, {"angle": 1}, "deg"),
    "°": (math.pi / 180.0, {"angle": 1}, "deg"),
    "rad": (1.0, {"angle": 1}, "rad"),
    # Mass -> kg
    "mg": (1e-6, {"mass": 1}, "mg"),
    "g": (1e-3, {"mass": 1}, "g"),
    "kg": (1.0, {"mass": 1}, "kg"),
    "lb": (0.45359237, {"mass": 1}, "lb"),
    # Force -> N (kg*m/s^2)
    "n": (1.0, {"mass": 1, "length": 1, "time": -2}, "N"),
    "kn": (1e3, {"mass": 1, "length": 1, "time": -2}, "kN"),
    # Pressure / Stress -> Pa (kg/(m*s^2))
    "pa": (1.0, {"mass": 1, "length": -1, "time": -2}, "Pa"),
    "kpa": (1e3, {"mass": 1, "length": -1, "time": -2}, "kPa"),
    "mpa": (1e6, {"mass": 1, "length": -1, "time": -2}, "MPa"),
    "gpa": (1e9, {"mass": 1, "length": -1, "time": -2}, "GPa"),
    "bar": (1e5, {"mass": 1, "length": -1, "time": -2}, "bar"),
    "psi": (6894.757293168, {"mass": 1, "length": -1, "time": -2}, "psi"),
}

_QTY_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Zμ°^0-9]+)\s*$")


def parse_quantity(
    raw: Any,
    fallback_units: Optional[Dict[str, int]] = None,
) -> Tuple[float, Dict[str, int], Optional[str]]:
    """Parse a numeric value with optional units into `(si_value, units_dict, expression)`.

    Examples:
        `"10mm"` -> `(0.01, {"length": 1}, "10mm")`
        `"45 deg"` -> `(0.785398..., {"angle": 1}, "45deg")`
        `{"value": 8, "unit": "mm"}` -> `(0.008, {"length": 1}, "8mm")`
        `0.01` -> `(0.01, fallback_units or {}, None)`
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw), dict(fallback_units or {}), None

    if isinstance(raw, dict) and "value" in raw:
        num = float(raw["value"])
        unit = str(raw.get("unit", "")).strip()
        if not unit:
            return num, dict(fallback_units or {}), None
        key = unit.lower()
        if key not in _UNIT_TABLE:
            raise ValueError(f"Unsupported unit {unit!r}. Supported: {sorted(_UNIT_TABLE.keys())}")
        mult, udict, suffix = _UNIT_TABLE[key]
        expr = f"{raw['value']}{suffix}"
        return num * mult, dict(udict), expr

    if isinstance(raw, str):
        s = raw.strip()
        # Plain numeric string without unit
        try:
            return float(s), dict(fallback_units or {}), None
        except ValueError:
            pass
        m = _QTY_RE.match(s)
        if not m:
            raise ValueError(f"Cannot parse quantity {raw!r}. Use a number or e.g. '10mm', '45deg'.")
        num_str, unit_str = m.group(1), m.group(2)
        key = unit_str.lower()
        if key not in _UNIT_TABLE:
            raise ValueError(f"Unsupported unit {unit_str!r} in {raw!r}. Supported: {sorted(_UNIT_TABLE.keys())}")
        mult, udict, suffix = _UNIT_TABLE[key]
        return float(num_str) * mult, dict(udict), f"{num_str}{suffix}"

    raise ValueError(f"Unsupported numeric/quantity value: {raw!r}")
