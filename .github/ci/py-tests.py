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

# test_mods' link tests aren't here: they expect a Windows user without the symlink privilege,
# and the runner is an administrator (5 failed at bb0dcc1); they run in the local gate
# (tools/test-port.sh). The branch's own test in that module is named by id (port-a916d5d-F1).
MODULES = ['test_launcher_plugins', 'test_paramdefs', 'test_probe', 'test_script_edits',
           'test_mods.ModTests.test_scripts_only_mod_is_a_mod_with_no_files']
# Full id -> why it isn't run here.
DESELECT = {
    'test_probe.LoaderTests.test_illegal_instruction_reports_guest_offset':
        'expects a POSIX signal code; known Windows failure (port-b4ae1ae-N2)',
}
# Test id or class id prefix -> why it may skip here.
EXPECTED_SKIPS = {
    'test_paramdefs.GameTests.': 'reads the game files: no BB_TEST_GAME_DIR in CI',
    'test_script_edits.GameFileTests.': 'reads the game files: no BB_TEST_GAME_DIR in CI',
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
    # Every failing test by name, so the annotation (40 lines) has them all (port-bb0dcc1-N2).
    for test, _trace in result.failures + result.errors:
        problems.append(f'failed: {test.id()}')
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
