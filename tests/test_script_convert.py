# SPDX-License-Identifier: GPL-3.0-or-later
"""DarkScript3 text to event edit files (scripts/script_convert.py). The instruction table here
is a small one written for the test, in the EMEDF JSON shape (DarkScript3's own isn't shipped)."""
from paths import ROOT
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(ROOT / 'scripts'))
import script_convert as sc  # noqa: E402
import script_edits as se  # noqa: E402

# Four instructions, with the layouts script_edits' built-ins have, in the EMEDF shape (types:
# 0 u8, 3 s8, 5 s32, 6 f32), a // comment line as DarkScript3's files have, and two
# instructions of one name (never guessed). Every argument has a default, as in DarkScript3's
# own table (port-f4dfb7e-F1).
EMEDF = '''{
  "main_classes": [
    {"index": 3, "instrs": [{"name": "IF Event Flag", "index": 0, "args": [
      {"type": 3, "enum_name": "Condition Group", "default": 0}, {"type": 0, "enum_name": "ON/OFF/CHANGE", "default": 0},
      {"type": 0, "enum_name": "Target Event Flag Type", "default": 0}, {"type": 5, "default": 0}]}]},
    {"index": 1001, "instrs": [{"name": "WAIT Fixed Time (Seconds)", "index": 0, "args": [{"type": 6, "default": 0.0}]}]},
    {"index": 2003, "instrs": [{"name": "Set Event Flag", "index": 2, "args": [
      {"type": 5, "default": 0}, {"type": 0, "enum_name": "ON/OFF", "default": 0}]},
      {"name": "Twice", "index": 90, "args": []}, {"name": "Twice", "index": 91, "args": []}]}
  ],
  "enums": [
    {"name": "ON/OFF", "values": {"0": "OFF", "1": "ON"}},
    // a comment line
    {"name": "ON/OFF/CHANGE", "values": {"0": "OFF", "1": "ON", "2": "CHANGE"}},
    {"name": "Condition Group", "values": {"0": "MAIN", "1": "AND_01", "-1": "OR_01"}},
    {"name": "Target Event Flag Type", "values": {"0": "Event Flag", "1": "Event ID"}}
  ]
}'''

SCRIPT = '''// ==EMEVD==
// ==/EMEVD==
$Event(0, Default, function() {
    InitializeEvent(0, 12415990, 0);
    InitializeEvent(1, 12415992, 7);
    SetEventFlag(1, ON);
});

/* one the mod added */
$Event(12415990, Restart, function() {
    SetEventFlag(12401900, OFF); // first off
    WaitFixedTimeSeconds(2.5);
    IfEventFlag(MAIN, ON, TargetEventFlagType.EventFlag, 12400131);
    SetEventFlag(12401900, ON);
});

$Event(12415991, End, function() {
    SetEventFlag(12401901, ON);
});

$Event(12415992, Default, function(X0_4) {
    SetEventFlag(X0_4, ON);
});

$Event(12415993, Default, function() {
    if (EventFlag(12400131)) {
        SetEventFlag(12401903, ON);
    }
});
'''


class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.table = sc.load_emedf(EMEDF)

    def test_events_become_raw_instructions_that_script_edits_reads_as_its_names_do(self):
        text = sc.convert(SCRIPT, self.table, [12415990, 12415991])
        first, second = se.parse(text)
        self.assertEqual((first.id, first.restart, first.run), (12415990, 1, True))
        self.assertEqual((second.id, second.restart, second.run), (12415991, 2, False))
        named = se.parse('[[event]]\nid = 1\ninstructions = ["SetEventFlag(12401900, OFF)", '
                         '"IfEventFlag(MAIN, ON, EventFlag, 12400131)", "SetEventFlag(12401900, ON)"]')[0]
        self.assertEqual([first.instructions[i] for i in (0, 2, 3)], named.instructions)
        self.assertEqual(first.instructions[1], se.raw({'bank': 1001, 'id': 0, 'args': ['f32:2.5']}))
        self.assertIn('{ bank = 2003, id = 2, args = ["s32:12401900", "u8:0"] },  # SetEventFlag(12401900, OFF)', text)

    def test_what_isnt_converted_says_why(self):
        cases = {
            12415992: 'has parameters (X0_4)',
            12415993: 'is not a plain instruction (MattScript',
            999: 'event 999 is not in the file',
        }
        for event_id, why in cases.items():
            with self.subTest(event=event_id):
                with self.assertRaises(sc.ConvertError) as caught:
                    sc.convert(SCRIPT, self.table, [event_id])
                self.assertIn(why, str(caught.exception))
        bad = {
            'Twice();': 'not an instruction of this EMEDF (or two have that name)',
            'SetEventFlag(5);': 'takes 2 arguments, not 1',
            'SetEventFlag(5, MAYBE);': "'MAYBE' is not a number or one of its enum names",
            'SetEventFlag(1.5, ON);': "'1.5' is not a whole number",
            'Explode(1);': 'Explode: not an instruction',
        }
        for statement, why in bad.items():
            with self.subTest(statement=statement):
                with self.assertRaises(sc.ConvertError) as caught:
                    sc.convert(f'$Event(5, Default, function() {{ {statement} }});', self.table, [5])
                self.assertIn(why, str(caught.exception))
        # Started only by InitializeEvent(0, id, 0): another slot or arguments isn't "run".
        self.assertIn('run = false', sc.convert(SCRIPT.replace('function(X0_4)', 'function()')
                                                .replace('X0_4', '1'), self.table, [12415992]))

    def test_every_argument_must_be_written_though_the_table_has_defaults(self):
        # port-f4dfb7e-F1: filling defaults made IfEventFlag(MAIN, ON) wait on flag 0.
        for statement, why in (('WaitFixedTimeSeconds();', 'takes 1 arguments, not 0: write every one'),
                               ('SetEventFlag(5);', 'takes 2 arguments, not 1'),
                               ('IfEventFlag(MAIN, ON);', 'takes 4 arguments, not 2')):
            with self.subTest(statement=statement):
                with self.assertRaises(sc.ConvertError) as caught:
                    sc.convert(f'$Event(5, Default, function() {{ {statement} }});', self.table, [5])
                self.assertIn(why, str(caught.exception))

    def test_the_command_line_writes_the_edit_file_or_a_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            js, emedf = Path(temp) / 'm24_01_00_00.emevd.js', Path(temp) / 'bb.emedf.json'
            js.write_text(SCRIPT, encoding='utf-8')
            emedf.write_bytes(b'\xef\xbb\xbf' + EMEDF.encode())  # a BOM, as Windows tools write
            out = io.StringIO()
            with unittest.mock.patch('sys.stdout', out):
                self.assertEqual(sc.main([str(js), '--emedf', str(emedf), '--events', '12415990']), 0)
            self.assertEqual([e.id for e in se.parse(out.getvalue())], [12415990])
            err = io.StringIO()
            with unittest.mock.patch('sys.stderr', err):
                self.assertEqual(sc.main([str(js), '--emedf', str(emedf), '--events', '12415992']), 1)
            self.assertIn('script_convert: event 12415992 has parameters', err.getvalue())


if __name__ == '__main__':
    unittest.main()
