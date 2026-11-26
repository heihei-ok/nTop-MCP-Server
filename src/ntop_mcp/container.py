"""Lossless binary container reader/writer for nTop (.ntop) files.

Format specification (verified on nTop 5.x files):
- File header:
  - 8 bytes:  b"MAGIC%$1"
  - 8 bytes:  section_count (<Q)
  - 8 bytes:  reserved (<Q, usually 0)
  - section_count * 24 bytes: index entries, each:
      - 16 bytes: ASCII section name (null-padded)
      - 8 bytes:  cumulative end offset relative to data_start (<Q)
  - 80 bytes: zero padding (preserved verbatim on round-trip)
- Each section (128-byte header + payload):
  - 8 bytes:  b"MAGIC@@9"
  - 16 bytes: sec_type (null-padded ASCII, e.g. "JSON", "json", "ntopfn", "obj_container")
  - 16 bytes: sec_name (null-padded ASCII)
  - 8 bytes:  payload_len (<Q)
  - 80 bytes: reserved/padding (preserved verbatim on round-trip)
  - payload_len bytes: payload
- `ntopfn` sections contain sequential nested `MAGIC@@9` sub-sections (typically `fn` + `leaves`).
- `obj_container` (`leaves`) starts with an 8-byte uint64 prefix offset (normally 8) followed by
  one or more nested `MAGIC@@9` sub-sections (typically `index` of type `json`).
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple


FILE_MAGIC = b"MAGIC%$1"
SECTION_MAGIC = b"MAGIC@@9"
SECTION_HEADER_SIZE = 128
DEFAULT_HEADER_GAP = b"\x00" * 80
DEFAULT_SECTION_PAD = b"\x00" * 80


def _encode_fixed_str(text: str, length: int = 16) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > length:
        raise ValueError(f"Identifier {text!r} exceeds {length}-byte fixed field")
    return raw + b"\x00" * (length - len(raw))


def _decode_fixed_str(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


@dataclass
class MagicSection:
    """A single MAGIC@@9-framed section or sub-section."""

    sec_type: str
    sec_name: str
    payload: bytes
    header_pad: bytes = DEFAULT_SECTION_PAD

    @classmethod
    def parse(cls, buf: bytes, offset: int = 0) -> Tuple["MagicSection", int]:
        if offset + SECTION_HEADER_SIZE > len(buf):
            raise ValueError(f"Truncated section header at offset {offset:#x}")
        magic = buf[offset : offset + 8]
        if magic != SECTION_MAGIC:
            raise ValueError(f"Invalid section magic at {offset:#x}: {magic!r}")
        sec_type = _decode_fixed_str(buf[offset + 8 : offset + 24])
        sec_name = _decode_fixed_str(buf[offset + 24 : offset + 40])
        payload_len = struct.unpack_from("<Q", buf, offset + 40)[0]
        header_pad = buf[offset + 48 : offset + 128]
        payload_start = offset + SECTION_HEADER_SIZE
        payload_end = payload_start + payload_len
        if payload_end > len(buf):
            raise ValueError(
                f"Section {sec_name!r} payload extends past buffer "
                f"({payload_end} > {len(buf)})"
            )
        payload = buf[payload_start:payload_end]
        return cls(sec_type=sec_type, sec_name=sec_name, payload=payload, header_pad=header_pad), payload_end

    def to_bytes(self) -> bytes:
        pad = self.header_pad if len(self.header_pad) == 80 else DEFAULT_SECTION_PAD
        return (
            SECTION_MAGIC
            + _encode_fixed_str(self.sec_type, 16)
            + _encode_fixed_str(self.sec_name, 16)
            + struct.pack("<Q", len(self.payload))
            + pad
            + self.payload
        )

    def as_json(self) -> Any:
        return json.loads(self.payload.decode("utf-8"))

    def set_json(self, obj: Any) -> None:
        # nTop stores compact UTF-8 JSON without spaces after separators
        self.payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass
class NTopFnSection:
    """Parsed view of an `ntopfn` section (e.g., `main`, `sf0`, ...)."""

    sub_sections: List[MagicSection] = field(default_factory=list)
    trailing: bytes = b""

    @classmethod
    def from_payload(cls, payload: bytes) -> "NTopFnSection":
        subs: List[MagicSection] = []
        off = 0
        while off + SECTION_HEADER_SIZE <= len(payload) and payload[off : off + 8] == SECTION_MAGIC:
            sec, off = MagicSection.parse(payload, off)
            subs.append(sec)
        return cls(sub_sections=subs, trailing=payload[off:])

    def to_payload(self) -> bytes:
        return b"".join(s.to_bytes() for s in self.sub_sections) + self.trailing

    def get_sub(self, name: str) -> Optional[MagicSection]:
        for s in self.sub_sections:
            if s.sec_name == name:
                return s
        return None


@dataclass
class ObjContainer:
    """Parsed view of an `obj_container` payload (such as `leaves`)."""

    prefix_bytes: bytes
    sub_sections: List[MagicSection] = field(default_factory=list)
    trailing: bytes = b""

    @classmethod
    def from_payload(cls, payload: bytes) -> "ObjContainer":
        if len(payload) < 8:
            return cls(prefix_bytes=payload)
        first_offset = struct.unpack_from("<Q", payload, 0)[0]
        if first_offset < 8 or first_offset > len(payload):
            # Fallback: scan for MAGIC@@9
            idx = payload.find(SECTION_MAGIC)
            if idx == -1:
                return cls(prefix_bytes=payload)
            first_offset = idx
        prefix = payload[:first_offset]
        subs: List[MagicSection] = []
        off = first_offset
        while off + SECTION_HEADER_SIZE <= len(payload) and payload[off : off + 8] == SECTION_MAGIC:
            sec, off = MagicSection.parse(payload, off)
            subs.append(sec)
        return cls(prefix_bytes=prefix, sub_sections=subs, trailing=payload[off:])

    def to_payload(self) -> bytes:
        return self.prefix_bytes + b"".join(s.to_bytes() for s in self.sub_sections) + self.trailing

    def get_sub(self, name: str) -> Optional[MagicSection]:
        for s in self.sub_sections:
            if s.sec_name == name:
                return s
        return None


@dataclass
class NTopContainer:
    """Full lossless representation of an `.ntop` file container."""

    sections: List[MagicSection] = field(default_factory=list)
    reserved: int = 0
    header_gap: bytes = DEFAULT_HEADER_GAP
    source_path: Optional[Path] = None

    @classmethod
    def from_bytes(cls, data: bytes, source_path: Optional[Path] = None) -> "NTopContainer":
        if len(data) < 24 or data[:8] != FILE_MAGIC:
            raise ValueError(f"Not a valid .ntop container (expected magic {FILE_MAGIC!r}, got {data[:8]!r})")

        count, reserved = struct.unpack_from("<QQ", data, 8)
        table_end = 24 + count * 24
        if table_end > len(data):
            raise ValueError(f"Truncated section table: expected {count} entries ({table_end} bytes)")

        entries: List[Tuple[str, int]] = []
        pos = 24
        for _ in range(count):
            name = _decode_fixed_str(data[pos : pos + 16])
            end_rel = struct.unpack_from("<Q", data, pos + 16)[0]
            entries.append((name, end_rel))
            pos += 24

        # Locate start of first section (normally table_end + 80)
        if count == 0:
            data_start = min(table_end + 80, len(data))
        else:
            expected_start = table_end + 80
            if data[expected_start : expected_start + 8] == SECTION_MAGIC:
                data_start = expected_start
            else:
                found = data.find(SECTION_MAGIC, table_end)
                if found == -1:
                    raise ValueError("Could not locate first MAGIC@@9 section in .ntop file")
                data_start = found

        header_gap = data[table_end:data_start]

        sections: List[MagicSection] = []
        prev_end = 0
        for idx, (entry_name, end_rel) in enumerate(entries):
            start = data_start + prev_end
            end = data_start + end_rel
            sec, next_off = MagicSection.parse(data, start)
            if next_off != end:
                raise ValueError(
                    f"Section {idx} ({entry_name!r}) size mismatch: "
                    f"header says end={end:#x}, section ends at {next_off:#x}"
                )
            sections.append(sec)
            prev_end = end_rel

        return cls(sections=sections, reserved=reserved, header_gap=header_gap, source_path=source_path)

    @classmethod
    def load(cls, path: str | Path) -> "NTopContainer":
        p = Path(path)
        return cls.from_bytes(p.read_bytes(), source_path=p)

    def to_bytes(self) -> bytes:
        serialized_sections = [s.to_bytes() for s in self.sections]
        header = bytearray()
        header += FILE_MAGIC
        header += struct.pack("<QQ", len(self.sections), self.reserved)

        cumulative = 0
        for sec, blob in zip(self.sections, serialized_sections):
            cumulative += len(blob)
            header += _encode_fixed_str(sec.sec_name, 16)
            header += struct.pack("<Q", cumulative)

        header += self.header_gap
        return bytes(header) + b"".join(serialized_sections)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.to_bytes())
        return p

    def get_section(self, name: str) -> Optional[MagicSection]:
        for s in self.sections:
            if s.sec_name == name:
                return s
        return None

    def section_names(self) -> List[str]:
        return [s.sec_name for s in self.sections]
