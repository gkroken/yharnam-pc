# SPDX-License-Identifier: GPL-3.0-or-later
"""launcher/bbport_plugins.py: plugins read from their files, the host's load rules, plugins.ini
edits and the last run's log. The DLLs are built here, byte by byte, so the test needs no compiler
and can make broken ones too."""

from paths import ROOT
import re
import struct
import sys
import tempfile
from pathlib import Path
import unittest

sys.path.insert(0, str(ROOT / 'launcher'))
import bbport_plugins as bp  # noqa: E402

IMAGE_BASE = 0x180000000
DATA_RVA, DATA_AT = 0x1000, 0x400


class Dll:
    """A minimal PE32+ DLL: one data section holding the export table and whatever is added."""

    def __init__(self):
        self.blob = bytearray()
        self.exports = {}

    def add(self, data, align=8):
        self.blob += bytes(-len(self.blob) % align)
        rva = DATA_RVA + len(self.blob)
        self.blob += data
        return rva

    def text(self, s):
        return IMAGE_BASE + self.add(s.encode() + b'\0', 1) if s is not None else 0

    def info(self, name='demo', title='Demo', version='1.0.0', author='me', description='A demo.', api=16,
             flags=0):
        pointers = [self.text(s) for s in (name, title, version, author, description)]
        self.exports['bb_plugin_info'] = self.add(struct.pack('<II5Q', api, flags, *pointers))
        return self

    def options(self, entries):
        """entries: (type, key, label, help, default, min, max, choices); a zero entry ends them."""
        packed = [(kind, *(self.text(s) for s in (key, label, help_, default)), low, high, self.text(choices))
                  for kind, key, label, help_, default, low, high, choices in entries]
        table = b''.join(struct.pack('<I4xQQQQddQ', *entry) for entry in packed) + bytes(64)
        self.exports['bb_plugin_options'] = self.add(table)
        return self

    def function(self, name):
        self.exports[name] = self.add(b'\xc3')  # ret
        return self

    def bytes(self, machine=0x8664, magic=0x20B):
        names = sorted(self.exports)
        name_rvas = [self.add(n.encode() + b'\0', 1) for n in names]
        functions = self.add(b''.join(struct.pack('<I', self.exports[n]) for n in names))
        name_table = self.add(b''.join(struct.pack('<I', r) for r in name_rvas))
        ordinals = self.add(b''.join(struct.pack('<H', i) for i in range(len(names))))
        directory = self.add(struct.pack('<IIHHIIIIIII', 0, 0, 0, 0, 0, 1, len(names), len(names), functions,
                                         name_table, ordinals))
        raw = bytes(self.blob) + bytes(-len(self.blob) % 0x200)
        out = bytearray(DATA_AT)
        out[:2] = b'MZ'
        struct.pack_into('<I', out, 0x3C, 0x80)
        out[0x80:0x84] = b'PE\0\0'
        struct.pack_into('<HHIIIHH', out, 0x84, machine, 1, 0, 0, 0, 240, 0x2022)
        optional = 0x98
        struct.pack_into('<H', out, optional, magic)
        struct.pack_into('<Q', out, optional + 24, IMAGE_BASE)
        struct.pack_into('<I', out, optional + 108, 16)
        struct.pack_into('<II', out, optional + 112, directory, 40)
        section = optional + 240
        out[section:section + 8] = b'.data\0\0\0'
        struct.pack_into('<IIII', out, section + 8, len(raw), DATA_RVA, len(raw), DATA_AT)
        return bytes(out) + raw

    def plugin(self):
        return self.function('bb_plugin_image')


class Folder(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data, self.user = self.root / 'data', self.root / 'data' / 'user'

    def put(self, path, content):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path


class ReadPluginTests(Folder):
    def read(self, dll, name='demo.dll'):
        return bp.read_plugin(self.put(name, dll if isinstance(dll, bytes) else dll.bytes()))

    def test_info_and_options(self):
        dll = Dll().plugin().info(name='boss_rush', title='Boss rush', version='2.1', author='a hunter',
                                  description='Every boss, one after another.', flags=bp.OPT_IN | bp.GAMEPLAY)
        dll.options([(1, 'shops', 'Shuffle shops', None, 'true', 0, 0, None),
                     (3 | bp.OPTION_LIVE, 'speed', 'Game speed', 'How fast', '1.0', 0.25, 2.0, None),
                     (4, 'order', 'Order', None, 'story', 0, 0, 'story|random'),
                     (7, None, 'Rules', None, None, 0, 0, None)])
        p = self.read(dll)
        self.assertEqual(p.problem, '')
        self.assertEqual((p.name, p.title, p.version, p.author, p.description, p.api_version),
                         ('boss_rush', 'Boss rush', '2.1', 'a hunter', 'Every boss, one after another.', 16))
        self.assertTrue(p.opt_in and p.gameplay)
        self.assertEqual([(o['type'], o['live'], o['key'], o['label'], o['help'], o['default']) for o in p.options],
                         [('bool', False, 'shops', 'Shuffle shops', '', 'true'),
                          ('float', True, 'speed', 'Game speed', 'How fast', '1.0'),
                          ('choice', False, 'order', 'Order', '', 'story'),
                          ('heading', False, '', 'Rules', '', '')])
        self.assertEqual((p.options[1]['min'], p.options[1]['max']), (0.25, 2.0))
        self.assertEqual(p.options[2]['choices'], ['story', 'random'])

    def test_no_info_takes_the_file_name(self):
        p = self.read(Dll().function('bb_plugin_init'), 'Plain-One.dll')
        self.assertEqual((p.name, p.api_version, p.problem), ('Plain-One', 0, ''))

    def test_what_the_host_refuses(self):
        self.assertIn('not a plugin', self.read(Dll().info()).problem)
        self.assertIn("'_' or '-'", self.read(Dll().plugin().info(name='bad name!')).problem)
        self.assertIn('newer plugin API', self.read(Dll().plugin().info(api=bp.API_VERSION + 1)).problem)
        self.assertEqual(self.read(Dll().plugin().info(api=bp.API_VERSION)).problem, '')

    def test_broken_files_are_problems_not_crashes(self):
        good = Dll().plugin().info().bytes()
        cases = {
            'a text file': b'not a dll at all',
            'empty': b'',
            '32-bit': Dll().plugin().info().bytes(machine=0x14C),
            'PE32': Dll().plugin().info().bytes(magic=0x10B),
            'cut short': good[:0x300],
            'cut in the data': good[:DATA_AT + 16],
        }
        for what, data in cases.items():
            with self.subTest(what):
                self.assertTrue(self.read(data).problem.startswith("can't be read"), self.read(data).problem)

    def test_bad_pointers_and_endless_data(self):
        outside = Dll().plugin()
        outside.exports['bb_plugin_info'] = outside.add(struct.pack('<II5Q', 16, 0, IMAGE_BASE + 0x900000, 0, 0, 0, 0))
        below = Dll().plugin()
        below.exports['bb_plugin_info'] = below.add(struct.pack('<II5Q', 16, 0, 0x1234, 0, 0, 0, 0))
        endless = Dll().plugin()
        name = endless.add(b'x' * (bp.MAX_TEXT + 10), 1)
        endless.exports['bb_plugin_info'] = endless.add(struct.pack('<II5Q', 16, 0, IMAGE_BASE + name, 0, 0, 0, 0))
        options = Dll().plugin().info()
        options.exports['bb_plugin_options'] = options.add(
            struct.pack('<I4xQQQQddQ', 1, 0, 0, 0, 0, 0, 0, 0) * (bp.MAX_OPTIONS + 1) + bytes(64))
        for what, dll in {'outside': outside, 'below': below, 'endless string': endless, 'options': options}.items():
            with self.subTest(what):
                self.assertTrue(self.read(dll).problem.startswith("can't be read"), self.read(dll).problem)


class ScanTests(Folder):
    def dll(self, folder, file, **info):
        return self.put(f'{folder}/plugins/{file}', Dll().plugin().info(**info).bytes())

    def scan(self, ini='', online=False):
        self.put('data/user/plugins.ini', ini)
        return {p.file: p for p in bp.scan(self.data, self.user, online=online)}

    def test_folders_order_and_replacement(self):
        self.dll('data', 'b.dll', name='b_data')
        self.dll('data/user', 'B.DLL', name='b_user')
        self.dll('data', 'A.dll', name='a')
        self.dll('data', 'sub/inside.dll', name='inside')  # in a subfolder: not loaded
        self.put('data/plugins/readme.txt', 'x')
        plugins = bp.scan(self.data, self.user)
        self.assertEqual([p.name for p in plugins], ['a', 'b_user'])
        self.assertEqual(plugins[1].path, self.user / 'plugins' / 'B.DLL')

    def test_switches_opt_in_and_online(self):
        self.dll('data', 'off.dll', name='off')
        self.dll('data', 'optin.dll', name='optin', flags=bp.OPT_IN)
        self.dll('data', 'optin_on.dll', name='optin_on', flags=bp.OPT_IN)
        self.dll('data', 'play.dll', name='play', flags=bp.GAMEPLAY)
        self.dll('data', 'plain.dll', name='plain')
        found = self.scan('[plugins]\noff = 0\nOPTIN_ON = on\n')
        self.assertEqual({f: (p.loads, p.why, p.enabled) for f, p in found.items()}, {
            'off.dll': (False, 'off in plugins.ini', False),
            'optin.dll': (False, 'off until turned on in plugins.ini (opt-in)', False),
            'optin_on.dll': (True, '', True),
            'play.dll': (True, '', True),
            'plain.dll': (True, '', True),
        })
        online = self.scan('', online=True)
        self.assertEqual(online['play.dll'].why, 'changes the game, and this is online play')
        self.assertTrue(online['plain.dll'].loads)

    def test_duplicate_names_as_the_host(self):
        self.dll('data', 'a_first.dll', name='same')
        self.dll('data', 'b_second.dll', name='SAME')
        found = self.scan()
        self.assertTrue(found['a_first.dll'].loads)
        self.assertEqual(found['b_second.dll'].why, 'another plugin has its name')
        # The host compares with the plugins it loaded: a first one left out leaves the name free.
        self.dll('data', 'a_first.dll', name='same', flags=bp.OPT_IN)
        found = self.scan()
        self.assertFalse(found['a_first.dll'].loads)
        self.assertTrue(found['b_second.dll'].loads)

    def test_a_broken_dll_is_listed_with_its_problem(self):
        self.put('data/plugins/broken.dll', b'MZ nonsense')
        (p,) = bp.scan(self.data, self.user)
        self.assertFalse(p.loads)
        self.assertTrue(p.why.startswith("can't be read"))

    def test_api_version_is_the_hosts(self):
        header = (ROOT / 'include' / 'bbhost_plugin.h').read_text(encoding='utf-8')
        self.assertEqual(int(re.search(r'#define BB_PLUGIN_API_VERSION (\d+)', header).group(1)), bp.API_VERSION)


class IniTests(Folder):
    def ini(self, text, bom=False):
        return bp.PluginsIni(self.put('plugins.ini', (b'\xef\xbb\xbf' if bom else b'') + text.encode()))

    def test_reads_as_the_host(self):
        ini = self.ini('early = 1\n[Plugins]\nA = 1\na = 0  ; the last line wins\ncolour = red;dark\n'
                       'note = Wave #1\n[other]\na = 1\n', bom=True)
        self.assertEqual(ini.switch('a'), 0)
        self.assertIsNone(ini.get('plugins', 'early'))  # before any section: ignored
        self.assertEqual(ini.get('plugins', 'colour'), 'red;dark')
        self.assertEqual(ini.get('plugins', 'note'), 'Wave')
        self.assertEqual(ini.switch('missing'), -1)
        self.assertEqual(self.ini('[plugins]\nx = maybe\n').switch('x'), -1)
        for value, expected in (('TRUE', 1), ('On', 1), ('false', 0), ('OFF', 0)):
            self.assertEqual(self.ini(f'[plugins]\nx = {value}\n').switch('x'), expected)

    def test_set_changes_only_the_line_the_host_reads(self):
        text = '; my settings\r\n[plugins]\r\nfoo = 1\r\nFoo   =   1   ; on for now\r\n\r\n[foo]\r\nseed = 7\r\n'
        ini = self.ini(text)
        ini.set_switch('foo', False)
        self.assertEqual(ini.text(), text.replace('Foo   =   1   ;', 'Foo   =   0   ;'))
        self.assertEqual(ini.switch('foo'), 0)

    def test_set_adds_a_line_or_a_section(self):
        ini = self.ini('[plugins]\na = 1\n# end of plugins\n\n[a]\nx = 1\n')
        ini.set_switch('b', True)
        self.assertEqual(ini.text(), '[plugins]\na = 1\n# end of plugins\nb = 1\n\n[a]\nx = 1\n')
        ini = self.ini('[a]\r\nx = 1')  # no [plugins], no last newline: CRLF kept
        ini.set_switch('b', False)
        self.assertEqual(ini.text(), '[a]\r\nx = 1\r\n\r\n[plugins]\r\nb = 0\r\n')
        ini = bp.PluginsIni(self.root / 'none' / 'plugins.ini')
        ini.set_switch('b', True)
        self.assertEqual(ini.text(), '[plugins]\nb = 1\n')
        ini = self.ini('[plugins]\nb =\n')
        ini.set_switch('b', True)
        self.assertEqual(ini.text(), '[plugins]\nb = 1\n')

    def test_save_keeps_the_bom_and_leaves_no_temporary_file(self):
        ini = self.ini('[plugins]\na = 1\n', bom=True)
        ini.set_switch('a', False)
        ini.save()
        self.assertEqual(ini.path.read_bytes(), b'\xef\xbb\xbf[plugins]\na = 0\n')
        self.assertEqual([p.name for p in ini.path.parent.iterdir()], ['plugins.ini'])
        fresh = bp.PluginsIni(self.root / 'new' / 'plugins.ini')
        fresh.set_switch('a', True)
        fresh.save()
        self.assertEqual(bp.PluginsIni(fresh.path).switch('a'), 1)


class LastRunTests(unittest.TestCase):
    LOG = '\r\n'.join([
        'Engine hooks: 91 installed',
        r'Plugins: lua 0.1.0 (C:\games\bbport-windows\plugins\bb_lua.dll)',
        r'Plugins: plain (C:\games\bbport-windows\plugins\plain.dll)',
        r'Plugins: bad name! (C:\games\bbport-windows\plugins\badname.dll): left out, its name must be 1-63 letters',
        r'Plugins: future (C:\x\plugins\future.dll): left out, built for a newer plugin API than this host'
        r"'s (99 > 16)",
        r'Plugins: C:\x (old)\plugins\gone.dll: could not be loaded (Windows error 126)',
        r'Plugins: fails 1.2.3 (C:\x\plugins\fails.dll)',
        'Plugins: fails: bb_plugin_image returned 1; unloaded',
        'Plugins: 3 loaded',
        r'Plugin lua: hello: loaded (C:\u\plugins\lua\hello.lua)',
        'Plugin lua: mymod/main: stopped: main.lua:3: attempt to call a nil value',
        'Plugin lua: 1 script(s) running, 1 stopped',
    ])

    def test_states(self):
        run = bp.last_run(self.LOG)
        self.assertEqual(run['plugins'], {
            'lua': {'state': 'loaded', 'why': '', 'file': 'bb_lua.dll'},
            'plain': {'state': 'loaded', 'why': '', 'file': 'plain.dll'},
            'bad name!': {'state': 'left out', 'why': 'its name must be 1-63 letters', 'file': 'badname.dll'},
            'future': {'state': 'left out', 'why': "built for a newer plugin API than this host's (99 > 16)",
                       'file': 'future.dll'},
            'gone.dll': {'state': 'failed', 'why': 'Windows error 126', 'file': 'gone.dll'},
            'fails': {'state': 'unloaded', 'why': 'bb_plugin_image returned 1', 'file': 'fails.dll'},
        })
        self.assertEqual(run['scripts'], {
            'hello': {'state': 'running', 'why': ''},
            'mymod/main': {'state': 'stopped', 'why': 'main.lua:3: attempt to call a nil value'},
        })


class LuaScriptTests(Folder):
    def test_order_names_and_disabled_mods(self):
        for path in ('data/user/plugins/lua/b.lua', 'data/user/plugins/lua/A.lua', 'data/user/plugins/lua/n.txt',
                     'mods/m1/scripts/main.lua', 'mods/m2/scripts/main.lua'):
            self.put(path, '-- x')
        scripts = bp.lua_scripts(self.user, [('m2', self.root / 'mods/m2', False), ('m1', self.root / 'mods/m1', 1),
                                             ('none', self.root / 'mods/none', True)])
        self.assertEqual([(s['name'], s['mod'], s['enabled']) for s in scripts],
                         [('A', '', True), ('b', '', True), ('m2/main', 'm2', False), ('m1/main', 'm1', True)])


if __name__ == '__main__':
    unittest.main()
