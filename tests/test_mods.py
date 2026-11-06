from paths import ROOT
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('bbmods', ROOT / 'scripts/mods.py')
mods = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mods)


class ModTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / 'CUSA03173'
        self.assets = self.game / 'dvdroot_ps4' / 'chr'
        self.assets.mkdir(parents=True)
        (self.game / 'eboot.bin').write_bytes(b'original executable')
        (self.assets / 'a.dcx').write_bytes(b'original')
        (self.assets / 'b.dcx').write_bytes(b'untouched')
        self.moddir = self.root / 'mods'

    def mod(self, name, contents=b'mod', file='a.dcx'):
        root = self.moddir / name
        folder = root / 'dvdroot_ps4' / 'chr'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / file).write_bytes(contents)
        return root

    def test_merge_replacement_new_file_and_directory_listing(self):
        a = self.mod('A')
        self.mod('A', b'new', 'new.dcx')
        result = mods.build_overlay(self.game, self.root / 'out', [('A', a)])
        folder = result / 'dvdroot_ps4' / 'chr'
        self.assertEqual((folder / 'a.dcx').read_bytes(), b'mod')
        self.assertEqual((folder / 'b.dcx').read_bytes(), b'untouched')
        self.assertEqual((folder / 'new.dcx').read_bytes(), b'new')
        self.assertEqual({p.name for p in folder.iterdir()}, {'a.dcx', 'b.dcx', 'new.dcx'})
        self.assertEqual((self.assets / 'a.dcx').read_bytes(), b'original')
        self.assertFalse((self.assets / 'new.dcx').exists())
        self.assertEqual((result / 'eboot.bin').read_bytes(), b'original executable')

    def test_last_mod_wins_and_reordering_changes_winner(self):
        a, b = self.mod('A', b'A'), self.mod('B', b'B')
        for layers, winner in [([('A', a), ('B', b)], b'B'), ([('B', b), ('A', a)], b'A')]:
            result = mods.build_overlay(self.game, self.root / 'out', layers)
            self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), winner)

    def test_selection_disable_and_new_mod(self):
        for name in ['C', 'B', 'A']:
            self.mod(name)
        config = self.root / 'mods.json'
        config.write_text(json.dumps({'order': ['B', 'A', 'deleted'], 'disabled': ['A']}))
        self.assertEqual(mods.selected(self.moddir, config), ['B', 'C'])

    def test_no_mod_returns_original_game(self):
        self.assertEqual(mods.build_overlay(self.game, self.root / 'out', []), self.game)
        self.assertFalse((self.root / 'out').exists())

    def test_directory_conflict_does_not_modify_base(self):
        a = self.mod('A')
        (a / 'dvdroot_ps4/chr/a.dcx').unlink()
        nested = a / 'dvdroot_ps4/chr/b.dcx'
        nested.mkdir()
        (nested / 'child').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            mods.build_overlay(self.game, self.root / 'out', [('A', a)])
        self.assertEqual((self.assets / 'b.dcx').read_bytes(), b'untouched')
        self.assertEqual(list((self.root / 'out').iterdir()), [])

    def test_mod_symlinks_rejected(self):
        a = self.mod('A')
        (a / 'dvdroot_ps4/escape').symlink_to(self.assets, target_is_directory=True)
        with self.assertRaises(ValueError):
            mods.build_overlay(self.game, self.root / 'out', [('A', a)])

    @unittest.skipUnless(os.name == 'nt', 'Windows overlay links')
    def test_windows_overlay_files_report_their_size(self):
        # bb-probe stats with UCRT _stat64 and lists with FindFirstFileA: both report a file
        # symlink's own size (0), and the game panics loading it (FileTransferTask.cpp(865)).
        a = self.mod('A', b'replaced')
        result = mods.build_overlay(self.game, self.root / 'out', [('A', a)])
        folder = result / 'dvdroot_ps4' / 'chr'
        # DirEntry.stat(follow_symlinks=False) is FindFirstFile's data on Windows.
        sizes = {e.name: e.stat(follow_symlinks=False).st_size for e in os.scandir(folder)}
        self.assertEqual(sizes, {'a.dcx': len(b'replaced'), 'b.dcx': len(b'untouched')})
        self.assertFalse(any((folder / name).is_symlink() for name in sizes))

    @unittest.skipUnless(os.name == 'nt', 'Windows redirection trust policy')
    def test_overlay_under_redirection_trust_policy(self):
        # Armoury Crate (ROG Ally) starts games with the redirection trust policy: the process
        # and its children may not follow junctions a non-administrator made (WinError 448).
        (self.game / 'dvdroot_ps4' / 'map').mkdir()
        (self.game / 'dvdroot_ps4' / 'map' / 'm.dcx').write_bytes(b'map')
        a = self.mod('A', b'replaced')
        script = (
            "import ctypes, importlib.util, os, sys\n"
            "from pathlib import Path\n"
            "flag = ctypes.c_ulong(1)\n"
            "assert ctypes.windll.kernel32.SetProcessMitigationPolicy(16, ctypes.byref(flag), 4)\n"
            "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "result = m.build_overlay(sys.argv[2], sys.argv[3], [('A', Path(sys.argv[4]))])\n"
            "d = result / 'dvdroot_ps4'\n"
            "print((d / 'chr' / 'a.dcx').read_bytes().decode(), (d / 'chr' / 'b.dcx').read_bytes().decode(),\n"
            "      (d / 'map' / 'm.dcx').read_bytes().decode(), m.MIRROR_DIRECTORIES)\n")
        done = subprocess.run([sys.executable, '-I', '-c', script, str(ROOT / 'scripts/mods.py'),
                               str(self.game), str(self.root / 'out'), str(a)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ['replaced', 'untouched', 'map', 'True'])
        self.assertEqual((self.assets / 'a.dcx').read_bytes(), b'original')

    def test_overlay_moves_off_a_drive_without_links(self):
        # exFAT/FAT32: making a folder link fails with "[WinError 1] Incorrect function".
        a = self.mod('A')
        refused = self.root / 'out'
        real = mods.make_link

        def make_link(link, target):
            if refused in link.parents:
                raise OSError(22, 'Incorrect function')
            real(link, target)

        from unittest import mock
        with mock.patch.object(mods, 'make_link', make_link),                 mock.patch.object(mods.os, 'name', 'nt'),                 mock.patch.object(mods.tempfile, 'gettempdir', return_value=str(self.root / 'temp')):
            (self.root / 'temp').mkdir()
            result = mods.build_overlay(self.game, refused, [('A', a)])
        self.assertEqual(result.parent, (self.root / 'temp').resolve())
        self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'mod')
        self.assertEqual(list(refused.iterdir()), [])

    def wine_folder_links(self):
        """make_link as under Wine/Proton (GitHub issue #42): a folder link is made and lists, but
        what it shows is not the game's folder (here an empty one)."""
        decoy = self.root / 'decoy'
        decoy.mkdir()
        real = mods.make_link

        def make_link(link, target):
            if target.is_dir() and not mods.MIRROR_DIRECTORIES and self.game in target.parents:
                return real(link, decoy)
            return real(link, target)
        return make_link

    def test_overlay_whose_folder_links_do_not_open_copies_the_folders(self):
        (self.game / 'sce_module').mkdir()
        (self.game / 'sce_module' / 'libc.prx').write_bytes(b'libc')
        a = self.mod('A')
        from unittest import mock
        with mock.patch.object(mods, 'make_link', self.wine_folder_links()):
            result = mods.build_overlay(self.game, self.root / 'out', [('A', a)])
        self.assertEqual((result / 'sce_module/libc.prx').read_bytes(), b'libc')
        self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'mod')
        self.assertEqual((result / 'dvdroot_ps4/chr/b.dcx').read_bytes(), b'untouched')
        self.assertEqual(len(list((self.root / 'out').iterdir())), 1)  # the first try removed

    def test_overlay_without_working_links_starts_the_game_without_mods(self):
        (self.game / 'sce_module').mkdir()
        (self.game / 'sce_module' / 'libc.prx').write_bytes(b'libc')
        a = self.mod('A')
        from unittest import mock
        with mock.patch.object(mods, 'make_link', self.wine_folder_links()), \
                mock.patch.object(mods.os, 'link', side_effect=OSError(1, 'Incorrect function')), \
                mock.patch.object(mods.tempfile, 'gettempdir', return_value=str(self.root / 'temp')):
            (self.root / 'temp').mkdir()
            result = mods.build_overlay(self.game, self.root / 'out', [('A', a)])
        self.assertEqual(result, self.game.resolve())  # no copy of the whole game either
        self.assertEqual(list((self.root / 'out').iterdir()), [])
        self.assertEqual(list((self.root / 'temp').iterdir()), [])

    def test_executable_replacement_rejected(self):
        a = self.mod('A')
        (a / 'eboot.bin').write_bytes(b'unsupported')
        with self.assertRaises(ValueError):
            mods.build_overlay(self.game, self.root / 'out', [('A', a)])

    def test_shadps4_sibling_folder_and_global_disable(self):
        legacy = Path(str(self.game) + '-mods')
        folder = legacy / 'dvdroot_ps4/chr'
        folder.mkdir(parents=True)
        (folder / 'a.dcx').write_bytes(b'legacy')
        for enabled, expected in [('1', b'legacy'), ('0', b'original')]:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/mods.py'), str(self.game),
                '--out', str(self.root / 'out'), '--mods-dir', str(self.moddir), '--enabled', enabled],
                capture_output=True, text=True, check=True)
            self.assertEqual((Path(result.stdout.strip()) / 'dvdroot_ps4/chr/a.dcx').read_bytes(), expected)

    def test_shadps4_sibling_folder_of_symlinks_is_left_out(self):
        # BBLauncher's CUSA03173-mods: symlinks into its own Mods-Active folder.
        legacy = Path(str(self.game) + '-mods')
        folder = legacy / 'dvdroot_ps4/chr'
        folder.mkdir(parents=True)
        target = self.root / 'elsewhere.dcx'
        target.write_bytes(b'linked')
        try:
            (folder / 'a.dcx').symlink_to(target)
        except OSError:
            self.skipTest('creating symlinks needs Developer Mode on Windows')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/mods.py'), str(self.game),
            '--out', str(self.root / 'out'), '--mods-dir', str(self.moddir), '--enabled', '1'],
            capture_output=True, text=True, check=True)
        self.assertEqual((Path(result.stdout.strip()) / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'original')
        self.assertIn('left out', result.stderr)

    def test_empty_shadps4_sibling_folder_is_left_out(self):
        # What BBLauncher leaves with every mod deactivated: the folder, without game files.
        Path(str(self.game) + '-mods').mkdir()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/mods.py'), str(self.game),
            '--out', str(self.root / 'out'), '--mods-dir', str(self.moddir), '--enabled', '1'],
            capture_output=True, text=True, check=True)
        self.assertEqual((Path(result.stdout.strip()) / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'original')
        self.assertIn('left out', result.stderr)

    def test_generated_files_lie_under_player_mods_and_apply_with_mods_off(self):
        generated = self.root / 'generated' / 'feature'
        (generated / 'dvdroot_ps4/chr').mkdir(parents=True)
        (generated / 'dvdroot_ps4/chr/a.dcx').write_bytes(b'generated')
        (generated / 'dvdroot_ps4/chr/b.dcx').write_bytes(b'generated b')
        self.mod('Player', b'player')
        def run(enabled):
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/mods.py'), str(self.game),
                '--out', str(self.root / 'out'), '--mods-dir', str(self.moddir), '--enabled', enabled,
                '--generated', str(generated)], capture_output=True, text=True, check=True)
            return Path(result.stdout.strip()) / 'dvdroot_ps4/chr'
        self.assertEqual((run('1') / 'a.dcx').read_bytes(), b'player')
        self.assertEqual((run('1') / 'b.dcx').read_bytes(), b'generated b')
        self.assertEqual((run('0') / 'a.dcx').read_bytes(), b'generated')
        self.assertEqual((self.assets / 'a.dcx').read_bytes(), b'original')

    def test_app0_wrapped_mod_discovered(self):
        self.moddir.mkdir()
        (self.moddir / 'Wrapped/app0/dvdroot_ps4').mkdir(parents=True)
        self.assertEqual(mods.discover(self.moddir), ['Wrapped'])

    def test_other_case_replaces_the_game_file(self):
        root = self.moddir / 'Upper'
        (root / 'DVDROOT_PS4/Chr').mkdir(parents=True)
        (root / 'DVDROOT_PS4/Chr/A.DCX').write_bytes(b'upper')
        result = mods.build_overlay(self.game, self.root / 'out', [('Upper', root)])
        folder = result / 'dvdroot_ps4/chr'
        self.assertEqual((folder / 'a.dcx').read_bytes(), b'upper')
        self.assertEqual({p.name for p in folder.iterdir()}, {'a.dcx', 'b.dcx'})
        self.assertEqual({p.name for p in result.iterdir()}, {'dvdroot_ps4', 'eboot.bin'})

    def test_wrapped_and_bare_layouts(self):
        layouts = {'Title': 'CUSA03173/dvdroot_ps4/chr', 'Archive': 'Archive v1.2/dvdroot_ps4/chr',
                   'Bare': 'chr', 'Nested': 'Nested/app0/dvdroot_ps4/chr'}
        for name, path in layouts.items():
            folder = self.moddir / name / path
            folder.mkdir(parents=True)
            (folder / 'a.dcx').write_bytes(name.encode())
        (self.moddir / 'Archive/readme.txt').write_text('notes')
        (self.moddir / 'Junk/stuff').mkdir(parents=True)
        self.assertEqual(mods.discover(self.moddir), ['Archive', 'Bare', 'Nested', 'Title'])
        for name in layouts:
            with self.subTest(name=name):
                result = mods.build_overlay(self.game, self.root / 'out',
                                            [(name, self.moddir / name)])
                self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), name.encode())

    def test_mod_site_layouts(self):
        # As mods come from mod sites: notes without a suffix and previews beside the wrapper,
        # two wrappers deep, and the game's folders next to screenshots.
        layouts = {'Readme': 'Readme v1/dvdroot_ps4/chr', 'Deep': 'Deep/Deep v2/dvdroot_ps4/chr',
                   'Shots': 'chr'}
        for name, path in layouts.items():
            folder = self.moddir / name / path
            folder.mkdir(parents=True)
            (folder / 'a.dcx').write_bytes(name.encode())
        (self.moddir / 'Readme/README').write_text('notes')
        (self.moddir / 'Readme/preview.webp').write_bytes(b'image')
        (self.moddir / 'Readme/Nexus page.url').write_text('[InternetShortcut]')
        (self.moddir / 'Shots/Screenshots').mkdir()
        (self.moddir / 'Shots/Screenshots/1.png').write_bytes(b'image')
        (self.moddir / 'Shots/readme.txt').write_text('notes')
        self.assertEqual(mods.discover(self.moddir), ['Deep', 'Readme', 'Shots'])
        for name in layouts:
            with self.subTest(name=name):
                result = mods.build_overlay(self.game, self.root / 'out',
                                            [(name, self.moddir / name)])
                self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), name.encode())
                self.assertFalse((result / 'dvdroot_ps4/Screenshots').exists())
                self.assertFalse((result / 'dvdroot_ps4/readme.txt').exists())

    def test_unrecognized_folders_say_why(self):
        self.moddir.mkdir()
        (self.moddir / 'Zipped').mkdir()
        (self.moddir / 'Zipped/mod.zip').write_bytes(b'PK')
        (self.moddir / 'Two/A/dvdroot_ps4').mkdir(parents=True)
        (self.moddir / 'Two/B/dvdroot_ps4').mkdir(parents=True)
        (self.moddir / 'Two/A/dvdroot_ps4/x').write_bytes(b'x')
        (self.moddir / 'Empty').mkdir()
        found = dict(mods.unrecognized(self.moddir))
        self.assertEqual(sorted(found), ['Empty', 'Two', 'Zipped'])
        self.assertIn('archive', found['Zipped'])
        self.assertIn('several folders', found['Two'])
        self.assertEqual(found['Empty'], 'empty')

    def test_scripts_only_mod_is_a_mod_with_no_files(self):
        # bb_lua loads <mod>/scripts/*.lua: such a mod is listed (turned off and ordered in the
        # launcher), links nothing, and its scripts folder doesn't hide a wrapped mod's files.
        (self.moddir / 'Lua/scripts').mkdir(parents=True)
        (self.moddir / 'Lua/scripts/main.lua').write_text('bb.log("hi")')
        (self.moddir / 'Both/Both v1/dvdroot_ps4/chr').mkdir(parents=True)
        (self.moddir / 'Both/Both v1/dvdroot_ps4/chr/a.dcx').write_bytes(b'both')
        (self.moddir / 'Both/scripts').mkdir()
        (self.moddir / 'Both/scripts/main.lua').write_text('bb.log("both")')
        (self.moddir / 'NoLua/scripts').mkdir(parents=True)
        (self.moddir / 'NoLua/scripts/notes.txt').write_text('not a script')
        (self.moddir / 'Deeper/lua').mkdir(parents=True)
        (self.moddir / 'Deeper/lua/main.lua').write_text('bb.log("where bb_lua never looks")')
        self.assertEqual(mods.discover(self.moddir), ['Both', 'Lua'])
        found = dict(mods.unrecognized(self.moddir))
        self.assertEqual(sorted(found), ['Deeper', 'NoLua'])
        self.assertIn('Deeper/scripts/', found['Deeper'])
        self.assertEqual(list(mods.mod_files(self.moddir / 'Lua')), [])
        result = mods.build_overlay(self.game, self.root / 'out',
                                    [('Lua', self.moddir / 'Lua'), ('Both', self.moddir / 'Both')])
        self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'both')
        self.assertFalse((result / 'scripts').exists())
        self.assertEqual(mods.build_overlay(self.game, self.root / 'out2', [('Lua', self.moddir / 'Lua')]),
                         self.game.resolve())

    def test_invalid_profile_fails(self):
        self.mod('A')
        config = self.root / 'mods.json'
        config.write_text('{"disabled":"A"}')
        with self.assertRaises(ValueError):
            mods.selected(self.moddir, config)

    def test_run_uses_overlay_propagates_exit_and_cleans_view(self):
        self.mod('A')
        python = self.root / 'python'
        python.write_text(f'#!{sys.executable}\nimport subprocess,sys\n'
            'if sys.argv[1] == "scripts/mods.py" or sys.argv[1] == "-c":\n'
            '    sys.exit(subprocess.call([sys.executable,*sys.argv[1:]]))\n')
        python.chmod(0o755)
        probe = self.root / 'probe'
        probe.write_text(f'#!{sys.executable}\nimport json,sys,os\nfrom pathlib import Path\n'
            'game=Path(sys.argv[sys.argv.index("--app0")+1])\n'
            'Path(os.environ["BB_DATA_DIR"],"mounted.json").write_text(json.dumps({\n'
            '"path":str(game),"content":(game/"dvdroot_ps4/chr/a.dcx").read_text()}))\n'
            'sys.exit(7)\n')
        probe.chmod(0o755)
        env = dict(os.environ, BB_PREBUILT='1', BB_PROBE=str(probe), PYTHON=str(python),
            BB_DATA_DIR=str(self.root), BB_GAME_DIR=str(self.game),
            BB_MODS_DIR=str(self.moddir), BB_MODS_ENABLED='1', BB_MODS_CONFIG=str(self.root/'mods.json'))
        result = subprocess.run(['bash', 'run.sh'], cwd=ROOT, env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 7, result.stderr)
        mounted = json.loads((self.root / 'mounted.json').read_text())
        self.assertEqual(mounted['content'], 'mod')
        self.assertFalse(Path(mounted['path']).exists())
        self.assertEqual((self.assets/'a.dcx').read_bytes(), b'original')


# BB_TEST_GAME_DIR=<CUSA03173>: the PC menu's strings merged into a mod's menu text.
GAME = Path(os.environ['BB_TEST_GAME_DIR']) if os.environ.get('BB_TEST_GAME_DIR') else None


@unittest.skipUnless(GAME, 'BB_TEST_GAME_DIR not set')
class PcMenuTextTests(unittest.TestCase):
    """Issue #48: a mod with its own menu.msgbnd.dcx hid the PC menu's strings (rows showed ids)."""

    def setUp(self):
        import menu_assets
        from fromsoft import Bnd4, dcx_compress, dcx_decompress
        self.m, self.Bnd4, self.dcx_decompress = menu_assets, Bnd4, dcx_decompress
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        relative = Path('msg') / 'engus' / menu_assets.MESSAGE_FILE
        self.relative = Path('dvdroot_ps4') / relative
        packed = (GAME / 'dvdroot_ps4' / relative).read_bytes()
        # The PC menu's layer, as enhancements.py makes it.
        self.pc_menu = self.root / 'pc-menu'
        made = menu_assets.build_menu_messages(packed, menu_assets.read_messages())
        (self.pc_menu / self.relative).parent.mkdir(parents=True)
        (self.pc_menu / self.relative).write_bytes(made)
        # A mod with its own menu text: the game's, with one string of its own.
        archive = Bnd4(dcx_decompress(packed))
        index = archive.ids.index(menu_assets.MENU_TEXT)
        ids = menu_assets.fmg_read(archive.files[index])
        ids[999990] = 'Change Appearance'.encode('utf-16-le')
        archive.files[index] = menu_assets.fmg_write(ids)
        self.mod = self.root / 'mods' / 'Appearance'
        (self.mod / self.relative).parent.mkdir(parents=True)
        (self.mod / self.relative).write_bytes(dcx_compress(archive.to_bytes(), template=packed))

    def menu_text(self, path):
        archive = self.Bnd4(self.dcx_decompress(Path(path).read_bytes()))
        return self.m.fmg_read(archive.files[archive.ids.index(self.m.MENU_TEXT)])

    def test_mod_menu_text_gains_the_pc_menu_strings(self):
        layers = [('pc-menu', self.pc_menu), ('Appearance', self.mod)]
        out = self.root / 'out'
        merged = mods.keep_pc_menu_text(layers, {'pc-menu'}, out)
        self.assertEqual(merged[-1], ('pc-menu-text', out / 'pc-menu-text'))
        text = self.menu_text(out / 'pc-menu-text' / self.relative)
        rows = self.m.read_messages()
        self.assertEqual(text[110006].decode('utf-16-le'), rows[110006][0])
        self.assertEqual(text[999990].decode('utf-16-le'), 'Change Appearance')
        # Unchanged sources: the merged layer is reused as it is.
        stamp = (out / 'pc-menu-text' / 'stamp.txt').read_text(encoding='utf-8')
        self.assertEqual(mods.keep_pc_menu_text(layers, {'pc-menu'}, out)[-1][0], 'pc-menu-text')
        self.assertEqual((out / 'pc-menu-text' / 'stamp.txt').read_text(encoding='utf-8'), stamp)

    def test_no_layer_without_a_mod_over_the_menu_text_or_without_the_pc_menu(self):
        out = self.root / 'out'
        self.assertEqual(mods.keep_pc_menu_text([('pc-menu', self.pc_menu)], {'pc-menu'}, out),
                         [('pc-menu', self.pc_menu)])
        self.assertEqual(mods.keep_pc_menu_text([('Appearance', self.mod)], set(), out),
                         [('Appearance', self.mod)])
        # A mod under the PC menu's layer is already covered by it.
        layers = [('Appearance', self.mod), ('pc-menu', self.pc_menu)]
        self.assertEqual(mods.keep_pc_menu_text(layers, {'pc-menu'}, out), layers)

    def test_a_copy_that_already_has_the_strings_is_used_as_it_is(self):
        layers = [('pc-menu', self.pc_menu), ('Copy', self.pc_menu)]
        self.assertEqual(mods.keep_pc_menu_text(layers, {'pc-menu'}, self.root / 'out'), layers)
