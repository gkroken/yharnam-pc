# SPDX-License-Identifier: GPL-3.0-or-later
"""CI: run the Python tests of the plugins branch's code from tests/.

Every test runs and must pass, except the ones in DESELECT (taken out by full id, with the
reason). A skip that isn't in EXPECTED_SKIPS fails the run, so a test that quietly stops
running (a missing bb-probe.exe, say) can't pass CI. On failure, the end of the output also
goes out as an error annotation, which the public check-runs API serves without a token.
"""
import io
from pathlib import Path
import sys
import unittest

MODULES = ['test_launcher_plugins', 'test_paramdefs', 'test_probe', 'test_mods', 'test_script_edits']
# Full id -> why it isn't run here.
DESELECT = {
    'test_probe.LoaderTests.test_illegal_instruction_reports_guest_offset':
        'expects a POSIX signal code; known Windows failure (port-b4ae1ae-N2)',
    'test_mods.ModTests.test_mod_symlinks_rejected':
        'errors instead of skipping without the symlink privilege; known Windows failure '
        '(tools/test-baseline.txt, port-a2866f9-N3)',
}
# Test id or class id prefix -> why it may skip here.
EXPECTED_SKIPS = {
    'test_paramdefs.GameTests.': 'reads the game files: no BB_TEST_GAME_DIR in CI',
    'test_mods.PcMenuTextTests.': 'reads the game files: no BB_TEST_GAME_DIR in CI',
    'test_script_edits.GameFileTests.': 'reads the game files: no BB_TEST_GAME_DIR in CI',
    # Skips itself where a symlink can't be made (Windows without Developer Mode or admin).
    'test_mods.ModTests.test_shadps4_sibling_folder_of_symlinks_is_left_out':
        'needs the symlink privilege',
}


def tests_of(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from tests_of(item)
        else:
            yield item


def main():
    tests_dir = Path(__file__).resolve().parent.parent.parent / 'tests'
    sys.path.insert(0, str(tests_dir))
    loaded = list(tests_of(unittest.defaultTestLoader.loadTestsFromNames(MODULES)))
    ids = {t.id() for t in loaded}
    problems = [f'{d} is deselected but no longer exists' for d in DESELECT if d not in ids]
    suite = unittest.TestSuite(t for t in loaded if t.id() not in DESELECT)
    for test_id, why in DESELECT.items():
        print(f'NOT RUN {test_id}: {why}')
    out = io.StringIO()
    result = unittest.TextTestRunner(stream=out, verbosity=2).run(suite)
    text = out.getvalue()
    print(text)
    for test, reason in result.skipped:
        if not any(test.id().startswith(p) for p in EXPECTED_SKIPS):
            problems.append(f'{test.id()} skipped unexpectedly: {reason}')
    if result.testsRun == 0:
        problems.append('no tests ran')
    for p in problems:
        print('PROBLEM', p)
    if problems or not result.wasSuccessful():
        tail = problems[:20] + text.splitlines()[-(40 - len(problems[:20])):]
        body = '%0A'.join(line.replace('%', '%25').replace('\r', '') for line in tail)
        print(f'::error title=Python tests failed::{body}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
