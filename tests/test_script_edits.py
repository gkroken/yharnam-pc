# SPDX-License-Identifier: GPL-3.0-or-later
"""Mods' event edits (scripts/script_edits.py), on a small event file made here: no game needed.
With BB_TEST_GAME_DIR, also on the game's own m24_01_00_00 (Central Yharnam)."""
from paths import ROOT
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(ROOT / 'scripts'))
import script_edits as se  # noqa: E402
from emevd import Emevd  # noqa: E402
from fromsoft import dcx_compress, dcx_decompress  # noqa: E402


def small_emevd():
    """An EMEVD64 with event 0 (one END) and event 50 (one END), no parameters."""
    rows = [(0, 1000, 4, b'\0\0\0\0'), (50, 1000, 4, b'\x01\0\0\0')]
    events = b''.join(struct.pack('<6q', eid, 1, i * 0x20, 0, 0xffffffff, 0)
                      for i, (eid, *_rest) in enumerate(rows))
    args = b''.join(a for *_r, a in rows)
    instructions = b''.join(struct.pack('<iiqqq', bank, code, len(a), i * 4, 0xffffffff)
                            for i, (_e, bank, code, a) in enumerate(rows))
    event_at = 0x90
    ins_at = event_at + len(events)
    arg_at = ins_at + len(instructions)
    end = arg_at + len(args)
    head = bytearray(0x90)
    head[:12] = b'EVD\0\0\xff\0\0\xcc\0\0\0'
    struct.pack_into('<I', head, 0x0c, end)
    for at, value in ((0x10, len(rows)), (0x18, event_at), (0x20, len(rows)), (0x28, ins_at),
                      (0x38, arg_at), (0x48, arg_at), (0x58, end), (0x68, end),
                      (0x70, len(args)), (0x78, arg_at), (0x88, end)):
        struct.pack_into('<q', head, at, value)
    data = bytes(head) + events + instructions + args
    assert Emevd(data).to_bytes() == data
    return data


EDIT_A = '''
[[event]]
id = 12405000
restart = "end"
instructions = [
    "IfEventFlag(MAIN, ON, EventFlag, 12400000)",
    "SetEventFlag(12405001, ON)",
    { bank = 2003, id = 2, args = ["s32:12405002", "u8:1"] },
]
'''
EDIT_B = '''
[[event]]
id = 12405100
run = false
instructions = ["EndUnconditionally(Restart)"]
'''


def ids(data):
    return [struct.unpack_from('<q', row)[0] for row in Emevd(data).events]


class ParseTests(unittest.TestCase):
    def test_named_and_raw_instructions_pack_as_the_game_reads_them(self):
        (event,) = se.parse(EDIT_A)
        self.assertEqual((event.id, event.restart, event.run), (12405000, 2, True))
        self.assertEqual(event.instructions, [
            (3, 0, struct.pack('<bBBxi', 0, 1, 0, 12400000)),
            (2003, 2, struct.pack('<iB3x', 12405001, 1)),
            (2003, 2, struct.pack('<iB3x', 12405002, 1)),
        ])
        # The same layouts the port's own event edits write (boss_rematches.instruction).
        import boss_rematches as br
        self.assertEqual(event.instructions[0], br._await_flag_on(12400000))
        self.assertEqual(event.instructions[1], br._set(12405001, True))
        self.assertEqual(se.named('IfConditionGroup(OR_02, PASS, AND_01)'),
                         (0, 0, struct.pack('<bBbx', -2, 1, 1)))
        self.assertEqual(se.raw({'bank': 1001, 'id': 0, 'args': ['f32:1.5']}),
                         (1001, 0, struct.pack('<f', 1.5)))

    def test_mistakes_are_refused_with_where_and_why(self):
        cases = {
            '[[event]]\nid = 1\ninstructions = ["SetEventFlag(5, MAYBE)"]': "'MAYBE' is not one of OFF, ON",
            '[[event]]\nid = 1\ninstructions = ["SetEventFlag(5)"]': 'takes 2 arguments, not 1',
            '[[event]]\nid = 1\ninstructions = ["Explode(5)"]': 'not a built-in instruction',
            '[[event]]\nid = 1\ninstructions = ["SetEventFlag(5000000000, ON)"]': 'is not a s32',
            '[[event]]\nid = 1\ninstructions = ["SetEventFlag(1.5, ON)"]': 'is not a s32',
            '[[event]]\nid = 1\ninstructions = [{bank = 1, id = 2, args = ["i32:3"]}]': 'type one of',
            '[[event]]\nid = 1\ninstructions = [{bank = -1, id = 2}]': 'bank and id',
            '[[event]]\nid = 0\ninstructions = ["EndUnconditionally(End)"]': 'id must be a number above 0',
            '[[event]]\nid = 1\nrestart = "never"\ninstructions = ["EndUnconditionally(End)"]': 'restart is one of',
            '[[event]]\nid = 1\ninstructions = []': 'at least one',
            '[[event]]\nid = 1\ncode = "os.system()"\ninstructions = ["EndUnconditionally(End)"]': 'unknown keys',
            '[[event]]\nid = 1\ninstructions = ["EndUnconditionally(End)"]\n'
            '[[event]]\nid = 1\ninstructions = ["EndUnconditionally(End)"]': 'added twice',
            'run = 1': 'unknown keys',
            '[[event]\n': 'not valid TOML',
        }
        for text, why in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(se.EditError) as caught:
                    se.parse(text)
                self.assertIn(why, str(caught.exception))


class BuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.original = dcx_compress(small_emevd())
        self.files = {'dvdroot_ps4/event/m24_01_00_00.emevd.dcx': self.original}
        self.lines = []

    def mod(self, name, text, file='m24_01_00_00'):
        folder = self.root / 'mods' / name
        (folder / 'edits/event').mkdir(parents=True, exist_ok=True)
        (folder / 'edits/event' / f'{file}.emevd.edit.toml').write_text(text, encoding='utf-8')
        return name, folder

    def build(self, mods):
        return se.build(lambda rel: self.files.get(rel.as_posix()), mods, self.root / 'out',
                        log=self.lines.append)

    def result(self):
        return dcx_decompress((self.root / 'out/dvdroot_ps4/event/m24_01_00_00.emevd.dcx').read_bytes())

    def test_two_mods_add_events_to_one_file_in_mod_order(self):
        out = self.build([self.mod('A', EDIT_A), self.mod('B', EDIT_B)])
        self.assertEqual(out, self.root / 'out')
        data = self.result()
        self.assertEqual(ids(data), [0, 50, 12405000, 12405100])
        script = Emevd(data)
        # A's event runs from the constructor; B's (run = false) doesn't.
        init = [i for i in script.event_instructions(0) if i[:2] == (2000, 0)]
        self.assertEqual(init, [(2000, 0, struct.pack('<iii', 0, 12405000, 0))])
        self.assertEqual(struct.unpack_from('<q', script.events[2], 40)[0], 2)  # restart = end
        self.assertEqual(script.event_instructions(12405100), [(1000, 4, b'\x01\0\0\0')])
        # Everything the file had is kept.
        self.assertEqual(script.event_instructions(50), [(1000, 4, b'\x01\0\0\0')])
        self.assertEqual(script.event_instructions(0)[0], (1000, 4, b'\0\0\0\0'))
        self.assertIn('Script edits: dvdroot_ps4/event/m24_01_00_00.emevd.dcx: A, B', self.lines)
        # The DCX header is the original's but for the sizes.
        written = (self.root / 'out/dvdroot_ps4/event/m24_01_00_00.emevd.dcx').read_bytes()
        self.assertEqual(written[:0x1c], self.original[:0x1c])

    def test_a_failing_mod_is_left_out_whole_and_the_others_apply(self):
        clash = '[[event]]\nid = 12405200\ninstructions = ["EndUnconditionally(End)"]\n' \
                '[[event]]\nid = 50\ninstructions = ["EndUnconditionally(End)"]\n'
        self.build([self.mod('A', EDIT_A), self.mod('Clash', clash), self.mod('Same', EDIT_A),
                    self.mod('B', EDIT_B)])
        self.assertEqual(ids(self.result()), [0, 50, 12405000, 12405100])  # no 12405200 either
        self.assertIn('Script edits: Clash: m24_01_00_00.emevd.edit.toml: event 50 is already in '
                      'the file; left out', self.lines)
        self.assertIn('Script edits: Same: m24_01_00_00.emevd.edit.toml: event 12405000 is already '
                      'in the file; left out', self.lines)

    def test_nothing_written_when_nothing_applies(self):
        self.assertIsNone(self.build([self.mod('Bad', 'nonsense = ')]))
        self.assertFalse((self.root / 'out').exists())
        self.assertIsNone(self.build([self.mod('Ghost', EDIT_A, file='m99_00_00_00')]))
        self.assertIn('Script edits: dvdroot_ps4/event/m99_00_00_00.emevd.dcx is not in the game; '
                      'left out: Ghost', self.lines)
        self.files['dvdroot_ps4/event/m24_01_00_00.emevd.dcx'] = dcx_compress(b'EVD\0 not really')
        self.assertIsNone(self.build([self.mod('A', EDIT_A)]))
        self.assertTrue(any("can't be read" in line for line in self.lines), self.lines)

    def test_only_edit_files_in_edits_event_count(self):
        name, folder = self.mod('A', EDIT_A)
        (folder / 'edits/event/notes.txt').write_text('not an edit')
        (folder / 'edits/event/M24_01_00_00.EMEVD.EDIT.TOML.bak').write_text(EDIT_B)
        self.assertEqual([p.name for _r, p in se.edit_files(folder)], ['m24_01_00_00.emevd.edit.toml'])


class ModsMergeTests(unittest.TestCase):
    """Through scripts/mods.py, as run.py starts it: edits go on top of the file mods."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.game = self.root / 'CUSA03173'
        (self.game / 'dvdroot_ps4/event').mkdir(parents=True)
        (self.game / 'eboot.bin').write_bytes(b'game')
        self.event = Path('dvdroot_ps4/event/m24_01_00_00.emevd.dcx')
        self.original = dcx_compress(small_emevd())
        (self.game / self.event).write_bytes(self.original)
        self.mods = self.root / 'mods'
        for name, text in (('A', EDIT_A), ('B', EDIT_B)):
            (self.mods / name / 'edits/event').mkdir(parents=True)
            (self.mods / name / 'edits/event/m24_01_00_00.emevd.edit.toml').write_text(text)
        # A file mod whose copy of the event file has one more event: the edits build on it.
        script = Emevd(small_emevd())
        script.append_event(777, [(1000, 4, b'\0\0\0\0')])
        replaced = self.mods / '0_File/dvdroot_ps4/event'
        replaced.mkdir(parents=True)
        (replaced / 'm24_01_00_00.emevd.dcx').write_bytes(dcx_compress(script.to_bytes()))

    def run_mods(self, enabled='1'):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/mods.py'), str(self.game),
                                 '--out', str(self.root / 'out'), '--mods-dir', str(self.mods),
                                 '--enabled', enabled],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip()), result.stderr

    def test_edit_mods_are_mods_and_their_events_go_on_top_of_the_file_mods(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import mods
        self.assertEqual(mods.discover(self.mods), ['0_File', 'A', 'B'])
        view, log = self.run_mods()
        self.assertEqual(ids(dcx_decompress((view / self.event).read_bytes())),
                         [0, 50, 777, 12405000, 12405100])
        self.assertIn('Script edits: dvdroot_ps4/event/m24_01_00_00.emevd.dcx: A, B', log)
        self.assertEqual((self.game / self.event).read_bytes(), self.original)
        self.assertFalse((view / 'edits').exists())
        # Mods off: neither the file mod nor the edits.
        view, log = self.run_mods('0')
        self.assertEqual(ids(dcx_decompress((view / self.event).read_bytes())), [0, 50])
        self.assertNotIn('Script edits', log)
        self.assertFalse((self.root / 'out/script_edits').exists())

    def test_the_folder_names_agree_with_mods_py(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import mods
        self.assertEqual((mods.EDITS_FOLDER, mods.EVENT_EDIT_SUFFIX), (se.EDITS_FOLDER, se.EVENT_SUFFIX))


GAME = Path(os.environ['BB_TEST_GAME_DIR']) if os.environ.get('BB_TEST_GAME_DIR') else None


@unittest.skipUnless(GAME, 'BB_TEST_GAME_DIR not set')
class GameFileTests(unittest.TestCase):
    def test_central_yharnam_takes_two_mods_events_and_round_trips(self):
        relative = Path('dvdroot_ps4/event/m24_01_00_00.emevd.dcx')
        original = (GAME / relative).read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mods = []
            for name, text in (('A', EDIT_A), ('B', EDIT_B)):
                (root / name / 'edits/event').mkdir(parents=True)
                (root / name / 'edits/event/m24_01_00_00.emevd.edit.toml').write_text(text)
                mods.append((name, root / name))
            se.build(lambda rel: (GAME / rel).read_bytes() if (GAME / rel).is_file() else None,
                     mods, root / 'out', log=lambda line: None)
            data = dcx_decompress((root / 'out' / relative).read_bytes())
        before = Emevd(dcx_decompress(original))
        after = Emevd(data)
        self.assertEqual(Emevd(data).to_bytes(), data)
        self.assertEqual(ids(data)[:-2], [struct.unpack_from('<q', r)[0] for r in before.events])
        self.assertEqual(ids(data)[-2:], [12405000, 12405100])
        for row in before.events[1:]:
            event_id = struct.unpack_from('<q', row)[0]
            self.assertEqual(after.event_instructions(event_id), before.event_instructions(event_id))


if __name__ == '__main__':
    unittest.main()
