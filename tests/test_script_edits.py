# SPDX-License-Identifier: GPL-3.0-or-later
"""Mods' event edits (scripts/script_edits.py), on a small event file made here: no game needed.
With BB_TEST_GAME_DIR, also on the game's own m24_01_00_00 (Central Yharnam)."""
from paths import ROOT
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

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


TALK = '''
[[option]]
script = "t241335"
text = 15000000
flag = 12401902
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
            '[[event]]\nid = 1\ninstructions = [{bank = 1, id = 2, args = ["u8:300"]}]': '300 is not a u8',
            '[[event]]\nid = 1\ninstructions = [{bank = 1, id = 2, args = ["s8:-129"]}]': '-129 is not a s8',
            '[[event]]\nid = 1\ninstructions = [{bank = 1001, id = 0, args = ["f32:1e40"]}]':
                'does not fit a f32',
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


class TalkParseTests(unittest.TestCase):
    def test_options_and_mistakes(self):
        self.assertEqual(se.parse_talk(TALK), [se.TalkOption('t241335', 15000000, 12401902, 1)])
        cases = {
            '[[option]]\nscript = "gilbert"\ntext = 1\nflag = 2': 'a talk script\'s name',
            '[[option]]\nscript = "t1"\ntext = -1\nflag = 2': 'text must be a number from 0',
            '[[option]]\nscript = "t1"\ntext = 1\nflag = 0': 'flag must be a number from 1',
            '[[option]]\nscript = "t1"\ntext = 1\nflag = 2\nmenu = 3': 'menu is 1 or 2',
            '[[option]]\nscript = "t1"\ntext = 1\nflag = 2\nrun = "x"': 'unknown keys',
            '[[event]]\nid = 1': 'unknown keys',
        }
        for text, why in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(se.EditError) as caught:
                    se.parse_talk(text)
                self.assertIn(why, str(caught.exception))


class BuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        original = bytearray(dcx_compress(small_emevd()))
        original[0x30] = 7  # the compression level field: kept from the original, not rewritten
        self.original = bytes(original)
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
        # The DCX header is the original's but for the two sizes at 0x1c (port-a2866f9-N2).
        written = (self.root / 'out/dvdroot_ps4/event/m24_01_00_00.emevd.dcx').read_bytes()
        self.assertEqual(written[:0x1c], self.original[:0x1c])
        self.assertEqual(written[0x24:0x4c], self.original[0x24:0x4c])
        self.assertEqual(written[0x30], 7)

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

    def test_an_edit_file_with_a_utf8_bom_applies(self):
        name, folder = self.mod('Bom', '')
        (folder / 'edits/event/m24_01_00_00.emevd.edit.toml').write_bytes(b'\xef\xbb\xbf' + EDIT_A.encode())
        self.build([(name, folder)])
        self.assertEqual(ids(self.result()), [0, 50, 12405000])

    def test_an_edit_file_in_utf16_applies(self):
        # tool-97b03e7-F1: Windows PowerShell 5.1's `>` and Notepad's "Unicode" write UTF-16.
        for encoding, bom in (('utf-16-le', b'\xff\xfe'), ('utf-16-be', b'\xfe\xff')):
            with self.subTest(encoding=encoding):
                name, folder = self.mod('U16' + encoding[-2:], '')
                (folder / 'edits/event/m24_01_00_00.emevd.edit.toml').write_bytes(bom + EDIT_A.encode(encoding))
                self.lines.clear()
                self.build([(name, folder)])
                self.assertEqual(ids(self.result()), [0, 50, 12405000], self.lines)

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
        (folder / 'edits/talk').mkdir()
        (folder / 'edits/talk/m24_01_00_00.talk.edit.toml').write_text(TALK)
        (folder / 'edits/talk/m24_01_00_00.emevd.edit.toml').write_text(EDIT_B)  # wrong folder
        self.assertEqual([(k, r.as_posix(), p.name) for k, r, p in se.edit_files(folder)], [
            ('event', 'dvdroot_ps4/event/m24_01_00_00.emevd.dcx', 'm24_01_00_00.emevd.edit.toml'),
            ('talk', 'dvdroot_ps4/script/talk/m24_01_00_00.talkesdbnd.dcx', 'm24_01_00_00.talk.edit.toml'),
        ])


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
        self.assertEqual([p.name for p in (view / 'dvdroot_ps4/event').iterdir()],
                         ['m24_01_00_00.emevd.dcx'])  # no .toml in the overlay (port-a2866f9-N2)
        # Mods off: neither the file mod nor the edits.
        view, log = self.run_mods('0')
        self.assertEqual(ids(dcx_decompress((view / self.event).read_bytes())), [0, 50])
        self.assertNotIn('Script edits', log)
        self.assertFalse((self.root / 'out/script_edits').exists())

    def test_a_value_that_does_not_pack_leaves_that_mod_out_and_the_game_starts(self):
        # port-a2866f9-F1: struct.pack raised OverflowError for f32:1e40 and mods.py exited 1.
        (self.mods / 'Bad/edits/event').mkdir(parents=True)
        (self.mods / 'Bad/edits/event/m24_01_00_00.emevd.edit.toml').write_text(
            '[[event]]\nid = 12405300\ninstructions = [{bank = 1001, id = 0, args = ["f32:1e40"]}]\n')
        view, log = self.run_mods()
        self.assertEqual(ids(dcx_decompress((view / self.event).read_bytes())),
                         [0, 50, 777, 12405000, 12405100])
        self.assertIn('Script edits: Bad: m24_01_00_00.emevd.edit.toml: event 12405300, instruction 1: '
                      '1e+40 does not fit a f32', log)

    def test_an_unexpected_error_leaves_that_mod_out(self):
        # Whatever a mod's data does, build() logs it and goes on (the class of port-a2866f9-F1).
        lines = []
        original = dcx_compress(small_emevd())
        root = self.root / 'unexpected'
        for name in ('Boom', 'Good'):
            (root / name / 'edits/event').mkdir(parents=True)
            (root / name / 'edits/event/m24_01_00_00.emevd.edit.toml').write_text(EDIT_B if name == 'Good' else 'x')
        kind = se.KINDS['event']
        reader = lambda text: (_ for _ in ()).throw(RecursionError('deep')) if text == 'x' else se.parse(text)  # noqa: E731
        with unittest.mock.patch.dict(se.KINDS, {'event': kind[:4] + (reader,) + kind[5:]}):
            se.build(lambda rel: original, [('Boom', root / 'Boom'), ('Good', root / 'Good')],
                     root / 'out', log=lines.append)
        self.assertIn('Script edits: Boom: m24_01_00_00.emevd.edit.toml: could not be applied '
                      '(RecursionError: deep); left out. Please report this.', lines)
        self.assertIn('Script edits: dvdroot_ps4/event/m24_01_00_00.emevd.dcx: Good', lines)

    def test_the_layer_never_stops_the_start(self):
        # port-bb0dcc1-N1: whatever escapes build() (here an error reading the layers' files),
        # script_edits_layer says so and gives no layer; mods.py goes on.
        sys.path.insert(0, str(ROOT / 'scripts'))
        import mods
        real = mods.resolved
        mods.resolved = lambda game, layers: (_ for _ in ()).throw(MemoryError('out of memory'))
        try:
            with unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                layer = mods.script_edits_layer(self.game, [('A', self.mods / 'A')], set(),
                                                self.root / 'out/script_edits')
        finally:
            mods.resolved = real
        self.assertEqual(layer, [])
        self.assertIn('Script edits: none applied (MemoryError: out of memory)', err.getvalue())

    def test_the_folder_names_agree_with_mods_py(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import mods
        self.assertEqual((mods.EDITS_FOLDER, mods.EVENT_EDIT_SUFFIX, mods.TALK_EDIT_SUFFIX),
                         (se.EDITS_FOLDER, se.EVENT_SUFFIX, se.TALK_SUFFIX))
        talk_only = self.root / 'mods/TalkOnly/edits/talk'
        talk_only.mkdir(parents=True)
        (talk_only / 'm24_01_00_00.talk.edit.toml').write_text(TALK)
        self.assertTrue(mods.is_mod(talk_only.parent.parent))


GAME = Path(os.environ['BB_TEST_GAME_DIR']) if os.environ.get('BB_TEST_GAME_DIR') else None


@unittest.skipUnless(GAME, 'BB_TEST_GAME_DIR not set')
class GameTalkTests(unittest.TestCase):
    """Talk options on the game's own talk scripts (t241335: Central Yharnam, its menu's entries
    1-4 and Leave, 50)."""

    @classmethod
    def setUpClass(cls):
        cls.talk = GAME / 'dvdroot_ps4/script/talk'
        cls.cy = dcx_decompress((cls.talk / 'm24_01_00_00.talkesdbnd.dcx').read_bytes())

    def menu(self, data, name='t241335'):
        archive = se.Bnd4(data)
        script = se.esd.parse(archive.files[archive.index('\\' + name + '.esd')])
        state = next(st for st in script.states if st.entry >= 0 and se.SHOW_MENU in
                     [(script.cmds[c].bank, script.cmds[c].id) for c in script.runs[st.entry].items])
        entry = [(script.cmds[c].bank, script.cmds[c].id,
                  [script.exprs[i].code for i in script.runs[script.cmds[c].args].items])
                 for c in script.runs[state.entry].items]
        conds = [script.conds[c] for c in script.runs[state.conds].items]
        return script, entry, conds

    def test_two_options_go_into_gilberts_menu_and_leave_as_leave_does(self):
        out = se.apply_talk(self.cy, se.parse_talk(TALK) + [se.TalkOption('t241335', 15000005, 12401903)])
        script, entry, conds = self.menu(out)
        lit = lambda v: se.esd.done(se.esd.lit(v))  # noqa: E731
        self.assertEqual(entry[-3:], [(1, 19, [lit(51), lit(15000000), lit(-1)]),
                                      (1, 19, [lit(52), lit(15000005), lit(-1)]),
                                      (1, 10, [lit(0), lit(0), lit(0)])])
        _s, _e, before = self.menu(self.cy)
        leave = before[-1].target  # the closed-or-Leave branch
        # The first still reads the choice; the new branches follow it, newest first.
        self.assertTrue(script.exprs[conds[0].eval].code.startswith(se.READ_CHOICE))
        for cond, flag, index in ((conds[1], 12401903, 52), (conds[2], 12401902, 51)):
            self.assertEqual(script.exprs[cond.eval].code, se.esd.done(b'\xaf' + se.esd.lit(index) + se.EQ))
            picked = script.states[cond.target]
            (cmd,) = [script.cmds[c] for c in script.runs[picked.entry].items]
            self.assertEqual((cmd.bank, cmd.id, [script.exprs[i].code for i in script.runs[cmd.args].items]),
                             (1, 11, [lit(flag), lit(1)]))
            (out_cond,) = [script.conds[c] for c in script.runs[picked.conds].items]
            self.assertEqual(script.states[out_cond.target].id, _s.states[leave].id)
        # Every other talk script in the file is unchanged.
        a, b = se.Bnd4(self.cy), se.Bnd4(out)
        changed = [n for n, x, y in zip(a.names, a.files, b.files) if x != y]
        self.assertEqual([n.replace(chr(92), '/').rsplit('/', 1)[-1] for n in changed], ['t241335.esd'])

    def test_every_usual_menu_of_the_game_takes_an_option(self):
        took, refused = 0, []
        for path in sorted(self.talk.glob('*.talkesdbnd.dcx')):
            archive = se.Bnd4(dcx_decompress(path.read_bytes()))
            for name, data in zip(archive.names, archive.files):
                script = se.esd.parse(data)
                count = sum(1 for st in script.states if st.entry >= 0 and se.SHOW_MENU in
                            [(script.cmds[c].bank, script.cmds[c].id) for c in script.runs[st.entry].items])
                for menu in range(1, count + 1):
                    short = name.replace(chr(92), '/').rsplit('/', 1)[-1][:-4]
                    try:
                        se.add_option(data, se.TalkOption(short, 15000000, 12401902, menu))
                        took += 1
                    except se.EditError as error:
                        refused.append((short, menu, str(error)))
        # 53 menus; the two of another shape (t100000, t290000: the choice isn't read first) refused.
        self.assertEqual(took, 51, refused)
        self.assertEqual(sorted(r[0] for r in refused), ['t100000', 't290000'])

    def test_menu_2_edits_the_second_menu_only(self):
        # port-27e99b4-N1: t210630 (Hunter's Dream) has two menus; menu = 2 is the second in the
        # file, and only it gains the entry.
        dream = dcx_decompress((self.talk / 'm21_00_00_00.talkesdbnd.dcx').read_bytes())

        def menus(data):
            archive = se.Bnd4(data)
            script = se.esd.parse(archive.files[archive.index('\\t210630.esd')])
            return [[(script.cmds[c].bank, script.cmds[c].id,
                      [script.exprs[i].code for i in script.runs[script.cmds[c].args].items])
                     for c in script.runs[st.entry].items]
                    for st in script.states if st.entry >= 0 and se.SHOW_MENU in
                    [(script.cmds[c].bank, script.cmds[c].id) for c in script.runs[st.entry].items]]

        before = menus(dream)
        self.assertEqual(len(before), 2)
        after = menus(se.apply_talk(dream, [se.TalkOption('t210630', 15000000, 12401902, menu=2)]))
        self.assertEqual(after[0], before[0])
        added = [c for c in after[1] if c not in before[1]]
        self.assertEqual([(b, i, args[1]) for b, i, args in added],
                         [(1, 19, se.esd.done(se.esd.lit(15000000)))])

    def test_the_menu_list_tells_menus_apart_and_says_which_take_an_option(self):
        found = {(script, number): (entries, why) for script, number, entries, why in se.menus(self.cy)}
        self.assertEqual(found[('t241335', 1)], ([(1, 14000100), (2, 14000101), (3, 14000102),
                                                  (4, 14000103), (50, 15000005)], None))
        dream = dcx_decompress((self.talk / 'm21_00_00_00.talkesdbnd.dcx').read_bytes())
        found = {(script, number): (entries, why) for script, number, entries, why in se.menus(dream)}
        self.assertEqual(found[('t210630', 1)][0], [(1, 15000150), (2, 15000005)])
        self.assertEqual(found[('t210630', 2)][0], [(1, 70012010), (2, 70012020), (3, 70012030), (4, 70012040)])
        hub = dcx_decompress((self.talk / 'm10_00_00_00.talkesdbnd.dcx').read_bytes())
        (_s, _n, _e, why), = [m for m in se.menus(hub) if m[0] == 't100000']
        self.assertEqual(why, "its menu isn't of the usual shape (the choice isn't read first)")
        # port-53830c3-N1: a menu (with an entry of its own) that leads straight into Gilbert's: its
        # entry is its own, not Gilbert's menu's.
        archive = se.Bnd4(self.cy)
        i = archive.index('\\t241335.esd')
        script = se.esd.parse(archive.files[i])
        group, menu = next((g, si) for g in script.groups for si in g.states
                           if script.states[si].entry >= 0 and se.SHOW_MENU in
                           [(script.cmds[c].bank, script.cmds[c].id) for c in script.runs[script.states[si].entry].items])
        lit = lambda v: se.esd.done(se.esd.lit(v))  # noqa: E731
        other = script.state(max(script.states[si].id for si in group.states) + 1)
        group.states.append(other)
        script.states[other].entry = script.run([script.cmd(1, 19, [lit(9), lit(777), lit(-1)]),
                                                 script.cmd(1, 10, [lit(0), lit(0), lit(0)])])
        script.states[other].conds = script.run([script.cond(menu, lit(1))])
        archive.files[i] = se.esd.serialize(script)
        found = {(s, n): e for s, n, e, _why in se.menus(archive.to_bytes()) if s == 't241335'}
        self.assertEqual(sorted(found.values()), sorted([
            [(1, 14000100), (2, 14000101), (3, 14000102), (4, 14000103), (50, 15000005)], [(9, 777)]]))
        out = io.StringIO()
        with unittest.mock.patch('sys.stdout', out):
            self.assertEqual(se.main([str(GAME), 'm24_01_00_00']), 0)
        self.assertIn('t241335 menu 1: takes an option; entries 1: text 14000100, 2: text 14000101', out.getvalue())
        with unittest.mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(se.main([str(GAME), 'm99_00_00_00']), 1)

    def test_refusals(self):
        # A menu with two branches for a closed menu (Gilbert's with its last one twice): which
        # one an added option should leave by is unknown, so it's refused.
        archive = se.Bnd4(self.cy)
        data = archive.files[archive.index('\\t241335.esd')]
        script, _entry, conds = self.menu(self.cy)
        state = next(st for st in script.states if st.entry >= 0 and se.SHOW_MENU in
                     [(script.cmds[c].bank, script.cmds[c].id) for c in script.runs[st.entry].items])
        items = list(script.runs[state.conds].items)
        state.conds = script.run(items + [script.cond(conds[-1].target, script.exprs[conds[-1].eval].code)])
        twice = se.esd.serialize(script)
        self.assertNotEqual(twice, data)
        with self.assertRaises(se.EditError) as caught:
            se.add_option(twice, se.TalkOption('t241335', 1, 2))
        self.assertIn('2 branches for a closed menu', str(caught.exception))
        with self.assertRaises(se.EditError) as caught:
            se.apply_talk(self.cy, [se.TalkOption('t999999', 1, 2)])
        self.assertIn('not a talk script in this file', str(caught.exception))
        with self.assertRaises(se.EditError) as caught:
            se.apply_talk(self.cy, [se.TalkOption('t241335', 1, 2, menu=2)])
        self.assertIn('has 1 menu(s), not 2', str(caught.exception))


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
