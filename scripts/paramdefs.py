#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The game's param definitions as a table the plugin host reads (src/plugin_params.c).

Reads dvdroot_ps4/paramdef/paramdef.paramdefbnd.dcx from the game folder given (the mods'
overlay when there is one): one 64-bit PARAMDEF (format 201) per param type, with each field's
internal name, primitive type, bit width ("flag:1") or array length ("pad[4]"). Rows are the
fields in order, bitfields packed LSB first into units of their primitive's size while they fit.
The layout follows droogie's bbhost (src/engine/paramdef.cpp, GPL-3.0-or-later).

Writes <out>/paramdefs.txt, one line each:
  T <param type> <row bytes>
  F <name> <primitive> <offset> <bytes> <count> <bit> <bits>
Nothing of the game's data but names and layouts; the values stay in the game's own tables.
"""
import argparse
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fromsoft import FormatError, Bnd4, dcx_decompress  # noqa: E402

ELEMENT = {'s8': 1, 'u8': 1, 'dummy8': 1, 'fixstr': 1, 's16': 2, 'u16': 2, 'fixstrW': 2, 's32': 4, 'u32': 4, 'f32': 4}
FORMAT_64 = 201
FIELD_BYTES = 0xd0


def _text(data, at, n):
    raw = data[at:at + n]
    end = raw.find(b'\0')
    return (raw if end < 0 else raw[:end]).decode('ascii', 'replace')


def parse(data):
    """(param type, row bytes, [(name, primitive, offset, bytes, count, bit, bits)]), or None
    for anything but the 64-bit format this build ships."""
    if len(data) < 0x38:
        return None
    count, field_bytes = struct.unpack_from('<HH', data, 0x08)
    fmt, = struct.unpack_from('<H', data, 0x2e)
    fields_at, = struct.unpack_from('<Q', data, 0x30)
    if fmt != FORMAT_64 or field_bytes != FIELD_BYTES or fields_at + count * field_bytes > len(data):
        return None
    param_type = _text(data, 0x0c, 0x20)
    if not param_type:
        return None
    fields, offset, unit = [], 0, None  # unit: [offset, bytes, bits used]
    for i in range(count):
        at = fields_at + i * field_bytes
        primitive = _text(data, at + 0x40, 8)
        byte_count, = struct.unpack_from('<I', data, at + 0x64)
        name = _text(data, at + 0x90, 0x20)
        bits, n = 0, 1
        if ':' in name:
            name, width = name.split(':', 1)
            bits = int(width or 0)
        if '[' in name:
            name, length = name.split('[', 1)
            n = max(1, int(length.rstrip(']') or 1))
        name = name.strip()
        size = ELEMENT.get(primitive, 0)
        if bits and size:
            if unit is None or unit[1] != size or unit[2] + bits > size * 8:
                if unit is not None:
                    offset += unit[1]
                unit = [offset, size, 0]
            fields.append((name, primitive, unit[0], size, 1, unit[2], bits))
            unit[2] += bits
        else:
            if unit is not None:
                offset += unit[1]
                unit = None
            total = byte_count or size * n
            fields.append((name, primitive, offset, total, n, 0, 0))
            offset += total
    if unit is not None:
        offset += unit[1]
    return param_type, offset, fields


def write_table(definitions, out):
    lines = []
    for param_type, row_bytes, fields in sorted(definitions):
        lines.append(f'T {param_type} {row_bytes}')
        for name, primitive, offset, size, count, bit, bits in fields:
            if name and ' ' not in name:
                lines.append(f'F {name} {primitive} {offset} {size} {count} {bit} {bits}')
    out.write_text('\n'.join(lines) + '\n', encoding='ascii', errors='replace')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('game', type=Path, help='the game folder (with dvdroot_ps4)')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    archive = args.game / 'dvdroot_ps4' / 'paramdef' / 'paramdef.paramdefbnd.dcx'
    try:
        bnd = Bnd4(dcx_decompress(archive.read_bytes()))
    except (OSError, FormatError) as error:
        print(f'Param definitions: none ({error}); plugins cannot use params by field name', file=sys.stderr)
        return 1
    definitions = [d for d in (parse(data) for data in bnd.files) if d]
    args.out.mkdir(parents=True, exist_ok=True)
    write_table(definitions, args.out / 'paramdefs.txt')
    print(f'Param definitions: {len(definitions)} of {len(bnd.files)} for plugins', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
