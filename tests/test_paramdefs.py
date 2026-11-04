"""scripts/paramdefs.py: the field table for plugins, from the game's own PARAMDEF files."""
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT  # noqa: E402

sys.path.insert(0, str(ROOT / 'scripts'))
import paramdefs  # noqa: E402
from fromsoft import Bnd4, dcx_decompress  # noqa: E402


def paramdef(param_type, fields):
    """A 64-bit PARAMDEF (format 201) with fields (display type, byte count, internal name)."""
    header = bytearray(0x40)
    struct.pack_into('<HH', header, 0x08, len(fields), 0xd0)
    header[0x0c:0x0c + len(param_type)] = param_type.encode()
    struct.pack_into('<H', header, 0x2e, 201)
    struct.pack_into('<Q', header, 0x30, 0x40)
    body = bytearray()
    for primitive, byte_count, name in fields:
        f = bytearray(0xd0)
        f[0x40:0x40 + len(primitive)] = primitive.encode()
        struct.pack_into('<I', f, 0x64, byte_count)
        f[0x90:0x90 + len(name)] = name.encode()
        body += f
    return bytes(header + body)


class ParseTests(unittest.TestCase):
    def test_bitfields_pack_lsb_first_into_units_of_their_size(self):
        data = paramdef('X_ST', [('s32', 4, 'a'), ('u8', 1, 'f1:1'), ('u8', 1, 'f2:3'), ('u8', 1, 'f3:5'),
                                 ('u16', 2, 'g:2'), ('u8', 3, 'pad[3]'), ('f32', 4, 'b')])
        param_type, row, fields = paramdefs.parse(data)
        self.assertEqual(param_type, 'X_ST')
        by = {f[0]: f for f in fields}
        self.assertEqual(by['a'][2:], (0, 4, 1, 0, 0))
        self.assertEqual(by['f1'][2:], (4, 1, 1, 0, 1))   # unit at 4, bit 0
        self.assertEqual(by['f2'][2:], (4, 1, 1, 1, 3))   # same unit, bits 1-3
        self.assertEqual(by['f3'][2:], (5, 1, 1, 0, 5))   # 4 + 5 > 8 bits: a new unit at 5
        self.assertEqual(by['g'][2:], (6, 2, 1, 0, 2))    # another width: a new u16 unit
        self.assertEqual(by['pad'][2:], (8, 3, 3, 0, 0))  # array of 3
        self.assertEqual(by['b'][2:], (11, 4, 1, 0, 0))
        self.assertEqual(row, 15)

    def test_other_formats_are_left_out(self):
        data = bytearray(paramdef('X_ST', [('s32', 4, 'a')]))
        struct.pack_into('<H', data, 0x2e, 104)
        self.assertIsNone(paramdefs.parse(bytes(data)))
        self.assertIsNone(paramdefs.parse(b'short'))

    def test_table_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'paramdefs.txt'
            paramdefs.write_table([paramdefs.parse(paramdef('X_ST', [('s32', 4, 'a'), ('u8', 1, 'f:1')]))], out)
            self.assertEqual(out.read_text(encoding='ascii').splitlines(),
                             ['T X_ST 5', 'F a s32 0 4 1 0 0', 'F f u8 4 1 1 0 1'])


@unittest.skipUnless(os.environ.get('BB_TEST_GAME_DIR'), 'BB_TEST_GAME_DIR not set')
class GameTests(unittest.TestCase):
    """Against the player's own 1.09 files: every definition parses, and each live table's row
    size equals its definition's (the check plugin_params.c also makes in game)."""

    def test_every_table_matches_its_definition(self):
        game = Path(os.environ['BB_TEST_GAME_DIR']) / 'dvdroot_ps4'
        bnd = Bnd4(dcx_decompress((game / 'paramdef' / 'paramdef.paramdefbnd.dcx').read_bytes()))
        defs = {d[0]: d for d in (paramdefs.parse(data) for data in bnd.files) if d}
        self.assertEqual(len(defs), len(bnd.files))
        tables = Bnd4(dcx_decompress((game / 'param' / 'gameparam' / 'gameparam.parambnd.dcx').read_bytes()))
        compared = 0
        for name, data in zip(tables.names, tables.files):
            rows, = struct.unpack_from('<H', data, 0x0a)
            param_type = data[0x0c:0x2c].split(b'\0')[0].decode()
            self.assertIn(param_type, defs, name)
            if rows >= 2:
                first, = struct.unpack_from('<Q', data, 0x48)
                second, = struct.unpack_from('<Q', data, 0x48 + 0x18)
                self.assertEqual(second - first, defs[param_type][1], name)
                compared += 1
        self.assertGreater(compared, 50)


if __name__ == '__main__':
    unittest.main()
