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
        # bb_plugin_info's 48 bytes start 8 bytes before the end of the image's one section (its
        # contents are padded to 0x200 bytes), so they run past it.
        straddling = Dll().plugin()
        straddling.exports['bb_plugin_info'] = DATA_RVA + 0x200 - 8
        self.assertEqual(struct.unpack_from('<I', straddling.bytes(), 0x98 + 240 + 8)[0], 0x200)
        straddling = Dll().plugin()
        straddling.exports['bb_plugin_info'] = DATA_RVA + 0x200 - 8
        for what, dll in {'outside': outside, 'below': below, 'endless string': endless, 'options': options,
                          'struct past the image': straddling}.items():
            with self.subTest(what):
                self.assertTrue(self.read(dll).problem.startswith("can't be read"), self.read(dll).problem)


    def test_no_cut_or_corruption_escapes_as_an_exception(self):
        """port-4409141-F1: a file cut inside its COFF header raised struct.error out of read_plugin().
        Every cut of a whole plugin, and seeded random corruption, must give a Plugin back."""
        import random
        dll = Dll().plugin().function('bb_plugin_init').info(flags=bp.GAMEPLAY)
        dll.options([(4, 'order', 'Order', 'help', 'story', 0, 0, 'story|random'), (2, 'n', 'N', None, '1', 0, 9, None)])
        whole = dll.bytes()
        self.assertEqual(self.read(whole).problem, '')
        self.assertTrue(self.read(whole[:0x80 + 4 + 10]).problem.startswith("can't be read"))  # in the COFF header
        path = self.root / 'fuzz.dll'  # never written: the bytes are passed in
        for cut in range(len(whole)):
            with self.subTest(cut=cut):
                self.assertIsInstance(bp.read_plugin(path, whole[:cut]), bp.Plugin)
        rng = random.Random(4409141)
        for i in range(3000):
            data = bytearray(whole)
            for _ in range(rng.randint(1, 6)):
                at = rng.randrange(0x200) if rng.random() < 0.5 else rng.randrange(len(data))
                data[at] = rng.randrange(256)
            with self.subTest(corruption=i):
                self.assertIsInstance(bp.read_plugin(path, bytes(data)), bp.Plugin)


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

    def test_the_host_cap_and_all_off(self):
        """port-4409141-N1: the host loads at most 64; BB_PLUGINS=0 loads none."""
        self.dll('data', 'a_off.dll', name='a_off')
        for i in range(bp.MAX_PLUGINS + 1):
            self.dll('data', f'p{i:02}.dll', name=f'p{i:02}')
        found = self.scan('[plugins]\na_off = 0\n')
        self.assertEqual(sum(p.loads for p in found.values()), bp.MAX_PLUGINS)
        self.assertEqual(found['a_off.dll'].why, 'off in plugins.ini')  # off ones don't count
        self.assertTrue(found['p63.dll'].loads)
        self.assertEqual(found['p64.dll'].why, '64 plugins are the most this host loads')
        everything_off = {p.file: p for p in bp.scan(self.data, self.user, all_off=True)}
        self.assertFalse(any(p.loads for p in everything_off.values()))
        self.assertEqual(everything_off['p00.dll'].why, 'no plugin loads (BB_PLUGINS=0)')

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

    def test_an_unreadable_file_reads_as_empty_and_is_never_written_over(self):
        """port-1a05644-F1: a plugins.ini that exists but can't be read (here a folder of that name)."""
        (self.root / 'plugins.ini').mkdir()
        (self.root / 'plugins.ini' / 'keep.txt').write_text('mine')
        ini = bp.PluginsIni(self.root / 'plugins.ini')
        self.assertTrue(ini.error.startswith("plugins.ini can't be read ("), ini.error)
        self.assertEqual((ini.switch('a'), ini.text()), (-1, ''))
        ini.set_switch('a', False)
        with self.assertRaises(OSError):
            ini.save()
        self.assertEqual((self.root / 'plugins.ini' / 'keep.txt').read_text(), 'mine')
        self.assertEqual(bp.PluginsIni(self.root / 'missing.ini').error, '')  # not there: no problem

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
        # Two files, one name (port-4409141-F2): the first loads, the second is left out.
        r'Plugins: weapon_tweak 1.0.0 (C:\x\port\plugins\weapon_tweak.dll)',
        r'Plugins: weapon_tweak (C:\x\port\user\plugins\zz_dup.dll): left out, another plugin has its name',
        r'Plugins: C:\x (old)\plugins\many.dll: left out, 64 plugins are the most this host loads',
        'Plugins: 3 loaded',
        r'Plugin lua: hello: loaded (C:\u\plugins\lua\hello.lua)',
        'Plugin lua: mymod/main: stopped: main.lua:3: attempt to call a nil value',
        'Plugin lua: 1 script(s) running, 1 stopped',
    ])

    def test_states(self):
        run = bp.last_run(self.LOG)
        self.assertEqual(run['plugins'], {
            'bb_lua.dll': {'name': 'lua', 'state': 'loaded', 'why': ''},
            'plain.dll': {'name': 'plain', 'state': 'loaded', 'why': ''},
            'badname.dll': {'name': 'bad name!', 'state': 'left out', 'why': 'its name must be 1-63 letters'},
            'future.dll': {'name': 'future', 'state': 'left out',
                           'why': "built for a newer plugin API than this host's (99 > 16)"},
            'gone.dll': {'name': '', 'state': 'failed', 'why': 'Windows error 126'},
            'fails.dll': {'name': 'fails', 'state': 'unloaded', 'why': 'bb_plugin_image returned 1'},
            'weapon_tweak.dll': {'name': 'weapon_tweak', 'state': 'loaded', 'why': ''},
            'zz_dup.dll': {'name': 'weapon_tweak', 'state': 'left out', 'why': 'another plugin has its name'},
            'many.dll': {'name': '', 'state': 'left out', 'why': '64 plugins are the most this host loads'},
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


class PageRowsTests(Folder):
    """page_rows(): what the Mods page shows, in the words it shows it."""

    def setUp(self):
        super().setUp()
        dll = lambda folder, file, **info: self.put(f'{folder}/plugins/{file}', Dll().plugin().info(**info).bytes())
        dll('data', 'bb_lua.dll', name='lua', title='', version='0.1.0', description='')
        dll('data', 'boss.dll', name='boss', title='Boss rush', version='2.0', description='Bosses.',
            flags=bp.GAMEPLAY)
        dll('data/user', 'later.dll', name='later', flags=bp.OPT_IN)
        dll('data/user', 'gone.dll', name='gone')
        self.put('data/user/plugins/broken.dll', b'MZ')
        self.put('data/user/plugins/lua/hello.lua', '-- x')
        self.put('data/user/plugins/lua/bad.lua', '-- x')
        self.put('mods/off_mod/scripts/main.lua', '-- x')
        self.log = '\n'.join([
            r'Plugins: lua 0.1.0 (C:\d\plugins\bb_lua.dll)',
            r'Plugins: boss 2.0 (C:\d\plugins\boss.dll)',
            'Plugins: boss: bb_plugin_image returned 1; unloaded',
            r'Plugins: C:\d\user\plugins\gone.dll: could not be loaded (Windows error 126)',
            r'Plugin lua: hello: loaded (C:\d\user\plugins\lua\hello.lua)',
            'Plugin lua: bad: stopped: bad.lua:1: oops',
        ])

    def rows(self, **kw):
        page = bp.page_rows(self.data, self.user, [('off_mod', self.root / 'mods/off_mod', False)], **kw)
        return page, {r['file']: r for r in page['plugins']}

    def test_plugins(self):
        page, rows = self.rows(log_text=self.log)
        self.assertEqual(list(rows), ['bb_lua.dll', 'boss.dll', 'broken.dll', 'gone.dll', 'later.dll'])
        lua, boss, broken, gone, later = rows.values()
        self.assertEqual((lua['title'], lua['next'], lua['lastRun'], lua['lastOk']),
                         ('lua', 'Loads at the next start', 'Last run: loaded', True))
        self.assertEqual((boss['title'], boss['version'], boss['description'], boss['gameplay']),
                         ('Boss rush', '2.0', 'Bosses.', True))
        self.assertEqual((boss['lastRun'], boss['lastOk']), ('Last run: stopped at its start (bb_plugin_image returned 1)', False))
        self.assertFalse(broken['canSwitch'])
        self.assertTrue(broken['next'].startswith("Can't load: can't be read"))
        self.assertEqual(gone['lastRun'], "Last run: Windows couldn't load it (Windows error 126)")
        self.assertEqual((later['enabled'], later['loads'], later['next']),
                         (False, False, 'Left out at the next start: off until turned on in plugins.ini (opt-in)'))
        self.assertEqual(page['luaNote'], '')
        _, online = self.rows(online=True)
        self.assertEqual(online['boss.dll']['next'], 'Left out at the next start: changes the game, and this is online play')
        switched_off = self.log + '\n' + r'Plugins: later (C:\d\user\plugins\later.dll): left out, off until turned on in plugins.ini (opt-in)'
        _, rows = self.rows(log_text=switched_off + '\n' + r'Plugins: x (C:\d\plugins\boss.dll): left out, another plugin has its name')
        self.assertTrue(rows['later.dll']['lastOk'])   # off by the player's choice: not a problem
        self.assertFalse(rows['boss.dll']['lastOk'])   # left out for another reason: one
        _, fresh = self.rows()  # no log yet
        self.assertEqual((fresh['boss.dll']['lastRun'], fresh['boss.dll']['lastOk']), ('', True))

    def test_scripts_and_the_lua_note(self):
        page, _ = self.rows(log_text=self.log)
        self.assertEqual([(s['name'], s['state'], s['ok']) for s in page['scripts']], [
            ('bad', 'Last run: stopped: bad.lua:1: oops', False),
            ('hello', 'Last run: running', True),
            ('off_mod/main', 'Off with its mod', True),
        ])
        self.put('data/user/plugins.ini', '[plugins]\nlua = off\n')
        page, _ = self.rows()
        self.assertEqual(page['luaNote'], 'No script runs: the Lua plugin is left out (off in plugins.ini).')
        (self.data / 'plugins' / 'bb_lua.dll').unlink()
        page, _ = self.rows()
        self.assertEqual(page['luaNote'], 'Lua scripts need bb_lua.dll in a plugins folder.')

    def test_mods_turned_off_turn_their_scripts_off(self):
        """tool-f91e77f-F1: the launcher's switch for all mods (BB_MODS_ENABLED) stops mods' scripts."""
        self.put('mods/on_mod/scripts/main.lua', '-- x')
        mods = [('on_mod', self.root / 'mods/on_mod', True), ('off_mod', self.root / 'mods/off_mod', False)]
        on = bp.page_rows(self.data, self.user, mods)['scripts']
        self.assertEqual([(s['name'], s['enabled'], s['state']) for s in on],
                         [('bad', True, ''), ('hello', True, ''), ('on_mod/main', True, ''),
                          ('off_mod/main', False, 'Off with its mod')])
        off = bp.page_rows(self.data, self.user, mods, mods_on=False)['scripts']
        self.assertEqual([(s['name'], s['enabled'], s['state']) for s in off],
                         [('bad', True, ''), ('hello', True, ''), ('on_mod/main', False, 'Off: mods are turned off'),
                          ('off_mod/main', False, 'Off: mods are turned off')])

    def test_an_unreadable_plugins_ini_is_a_line_on_the_page(self):
        page, rows = self.rows()
        self.assertEqual(page['problem'], '')
        (self.user / 'plugins.ini').mkdir()
        page, rows = self.rows()
        self.assertTrue(page['problem'].startswith("plugins.ini can't be read ("), page['problem'])
        self.assertTrue(page['problem'].endswith("so every plugin's switch is at its default."))
        self.assertTrue(rows['boss.dll']['loads'])        # as the host: defaults
        self.assertFalse(rows['later.dll']['loads'])      # an opt-in one stays off


if __name__ == '__main__':
    unittest.main()
