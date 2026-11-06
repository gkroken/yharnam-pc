#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Merge loose-file mods through links, preserving the original game and mod files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile

# Top-level folders of the game's dvdroot_ps4: a mod made of these is a dvdroot_ps4 itself.
GAME_FOLDERS = {'action', 'chr', 'event', 'facegen', 'font', 'map', 'menu', 'movie', 'msg', 'mtd',
                'obj', 'other', 'param', 'paramdef', 'parts', 'remo', 'script', 'sfx', 'shader',
                'sound'}


def child(folder, name):
    """`name` inside `folder`, matching an existing entry case-insensitively (mods made on
    Windows often differ from the game's lower-case names)."""
    folder = Path(folder)
    for entry in (folder.iterdir() if folder.is_dir() else ()):
        if entry.name.casefold() == name.casefold():
            return entry
    return folder / name


# Files that come with a mod without being part of it: notes, previews, links, licences.
EXTRA_SUFFIXES = {'', '.txt', '.md', '.ini', '.json', '.pdf', '.rtf', '.nfo', '.url', '.htm', '.html',
                  '.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}
ARCHIVE_SUFFIXES = {'.zip', '.7z', '.rar'}
# What a mod may carry at its top besides game files: Lua scripts (scripts/*.lua), which the
# plugins branch's bb_lua loads from there, and event edits (edits/event/*.emevd.edit.toml),
# which scripts/script_edits.py applies. They are never linked into the game, and a mod of
# only these is still a mod, so the launcher lists it, turns it off and orders it.
SCRIPTS_FOLDER = 'scripts'
EDITS_FOLDER = 'edits'  # script_edits.EDITS_FOLDER; not imported, so the launchers' Pythons
EVENT_EDIT_SUFFIX = '.emevd.edit.toml'  # without tomllib can still list mods
TALK_EDIT_SUFFIX = '.talk.edit.toml'


def has_scripts(folder):
    """Whether `folder` has Lua scripts where bb_lua loads a mod's: <mod>/scripts/*.lua. bb_lua
    skips mod folders whose name starts with '.', so their scripts don't count (port-fdab644-N1)."""
    if Path(folder).name.startswith('.'):
        return False
    scripts = child(folder, SCRIPTS_FOLDER)
    return scripts.is_dir() and any(e.is_file() and e.suffix.casefold() == '.lua'
                                    for e in scripts.iterdir())


def has_edits(folder):
    """Whether `folder` has edits: <mod>/edits/event/*.emevd.edit.toml or
    <mod>/edits/talk/*.talk.edit.toml."""
    edits = child(folder, EDITS_FOLDER)
    for kind, suffix in (('event', EVENT_EDIT_SUFFIX), ('talk', TALK_EDIT_SUFFIX)):
        found = child(edits, kind)
        if found.is_dir() and any(e.is_file() and e.name.casefold().endswith(suffix) for e in found.iterdir()):
            return True
    return False


def own_folders(folder):
    """The top folders of a mod that are its scripts or edits, not game files (case-folded)."""
    return ({SCRIPTS_FOLDER} if has_scripts(folder) else set()) | \
        ({EDITS_FOLDER} if has_edits(folder) else set())


def is_mod(folder):
    """A mod: game files in an accepted layout (content_root), or scripts or edits only."""
    return Path(folder).is_dir() and bool(content_root(folder) or own_folders(folder))


def content_root(folder, depth=0):
    """(root, prefix): where a mod's files are and the game path they go to, or None.

    Accepted layouts: <mod>/dvdroot_ps4, <mod>/app0/dvdroot_ps4, <mod>/<serial>/dvdroot_ps4, up to
    three wrapper folders around any of these (archives extracted into a folder of their name,
    beside notes and pictures), and the game's folders without dvdroot_ps4 (<mod>/chr,
    <mod>/parts, ...), also next to other folders (Screenshots, Optional)."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    # Every retail release (patches.BLOODBORNE_IDS).
    for wrapper in ('', 'app0', 'CUSA03173', 'CUSA00900', 'CUSA00207', 'CUSA00208', 'CUSA00299',
                    'CUSA01363', 'CUSA03179', 'CUSA03014', 'CUSA03023'):
        base = child(folder, wrapper) if wrapper else folder
        dvdroot = child(base, 'dvdroot_ps4')
        if dvdroot.is_dir():
            return base, ''
    entries = [e for e in folder.iterdir() if not e.name.startswith('.')]
    # The mod's own scripts or edits folder (with scripts or edits in it) isn't a wrapper around
    # its files: <mod>/<mod v1>/dvdroot_ps4 beside <mod>/scripts/*.lua. A "scripts" folder
    # without them may be one: <mod>/scripts/dvdroot_ps4 (port-fdab644-F1).
    own = own_folders(folder) if not depth else set()
    folders = [e for e in entries if e.is_dir() and e.name.casefold() not in own]
    if any(e.name.casefold() in GAME_FOLDERS for e in folders):
        return folder, 'dvdroot_ps4'
    if depth < 3 and len(folders) == 1 and not [e for e in entries if e.is_file() and
                                                e.suffix.casefold() not in EXTRA_SUFFIXES]:
        return content_root(folders[0], depth + 1)
    return None


def problem(folder):
    """Why `folder` is not a mod (content_root is None), for the launcher's list."""
    folder = Path(folder)
    files = [e for e in folder.rglob('*') if e.is_file()][:2000]
    if not files:
        return 'empty'
    if any(e.suffix.casefold() in ARCHIVE_SUFFIXES for e in files):
        return 'still an archive: extract it here'
    lua = [e for e in files if e.suffix.casefold() == '.lua']
    if lua:
        scripts = child(folder, SCRIPTS_FOLDER)
        if any(scripts in e.parents for e in lua):  # in a subfolder of scripts (port-fdab644-N2)
            return f'Lua scripts go directly in {folder.name}/{scripts.name}/, not in a subfolder'
        return f'Lua scripts go in {folder.name}/{SCRIPTS_FOLDER}/, directly in the mod folder'
    folders = [e for e in folder.iterdir() if e.is_dir() and not e.name.startswith('.')]
    if len(folders) > 1:
        return ('several folders: put each mod in its own folder in Mods (each with dvdroot_ps4, '
                'or folders like chr, parts, map)')
    return 'no game files: expected dvdroot_ps4, or folders like chr, parts, map'


def unrecognized(root):
    """(name, problem) of the folders of `root` that are not mods."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(((p.name, problem(p)) for p in root.iterdir()
                   if p.is_dir() and not p.name.startswith('.') and not is_mod(p)),
                  key=lambda item: (item[0].casefold(), item[0]))


def discover(root):
    """Named mods: folders of `root` with an accepted layout (is_mod)."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if is_mod(p)),
                  key=lambda name: (name.casefold(), name))


def selected(root, config):
    available = discover(root)
    if not config or not Path(config).is_file():
        return available
    settings = json.loads(Path(config).read_text(encoding='utf-8'))
    disabled_names = settings.get('disabled', [])
    if not isinstance(disabled_names, list) or not all(isinstance(n, str) for n in disabled_names):
        raise ValueError('Disabled mods must be a list of folder names')
    disabled = set(disabled_names)
    order = settings.get('order', [])
    if not isinstance(order, list) or not all(isinstance(n, str) for n in order):
        raise ValueError('Mod order must be a list of folder names')
    # New folders are enabled automatically and appended in alphabetical order.
    return list(dict.fromkeys(n for n in [*order, *available]
                             if n in available and n not in disabled))


def mod_files(folder):
    """(game path, source) of every file a mod replaces or adds under dvdroot_ps4."""
    layout = content_root(folder)
    if not layout and own_folders(folder):
        return  # scripts or edits only: nothing to link
    if not layout:
        raise ValueError(f'{folder}: expected dvdroot_ps4 (or chr/, parts/, ...) inside the mod folder')
    root, prefix = layout
    root = root.resolve()
    for directory, folders, files in os.walk(root, followlinks=False):
        directory = Path(directory)
        for name in [*folders, *files]:
            if (directory / name).is_symlink():
                raise ValueError(f'Mod symlinks are unsupported: {directory / name}')
        for name in sorted(files):
            source = directory / name
            if prefix and source.relative_to(root).parts[0].casefold() not in GAME_FOLDERS:
                continue  # notes and folders beside the game's (Screenshots, Optional)
            relative = Path(prefix) / source.relative_to(root) if prefix else source.relative_to(root)
            # Readme/metadata stay outside the mounted game. This loader handles assets;
            # executable patches use the existing patch compiler, with address validation.
            if relative.parts[0].casefold() != 'dvdroot_ps4':
                if relative.parts[0].casefold() in ('eboot.bin', 'sce_module', 'sce_sys'):
                    raise ValueError(f'Executable/system replacement is unsupported: {source}')
                continue
            if not source.is_file():
                raise ValueError(f'Not a regular mod file: {source}')
            yield relative, source


# Windows: files are always hard links (copies across volumes), never symlinks. The runtime's
# UCRT _stat64 and FindFirstFileA report a file symlink's own size (0), not its target's, and
# the game panics loading it (FileTransferTask.cpp(865)). Directories are symlinks, or
# junctions without the symlink privilege (Developer Mode off). Hard links and junctions are
# not symlinks, so the overlay remembers what it linked and where to.
#
# A process started with Windows' redirection trust policy (RedirectionGuard; Armoury Crate on
# the ROG Ally starts games this way, and children inherit it) refuses to follow a junction or
# symlink a non-administrator made: ERROR_UNTRUSTED_MOUNT_POINT. The first directory link is
# tried; if it is refused, every directory becomes a real folder of hard-linked files (about
# 6 s for the whole game on an SSD) for the rest of this overlay.
LINKED = {}
UNTRUSTED_MOUNT_POINT = 448
MIRROR_DIRECTORIES = False
MIRROR_GAME = None  # the game folder of the overlay being made


class UnresolvedLinks(OSError):
    """The overlay's links do not lead to the game's files."""


def _directory_link_followed(link, target):
    """Whether `link` shows `target`'s entries and its files open. Under Wine/Proton a folder
    link can be made and listed while the files in it are not found (GitHub issue #42: "No such
    file or directory: ...\\sce_module\\libc.prx")."""
    try:
        names = sorted(os.listdir(link))
        if names != sorted(os.listdir(target)):
            return False
    except OSError as error:
        if getattr(error, 'winerror', None) == UNTRUSTED_MOUNT_POINT:
            return False
        if not target.is_dir():
            raise
        return False
    first = next((name for name in names if (target / name).is_file()), None)
    return first is None or (link / first).is_file()


def _unresolved(overlay, game):
    """The first of the game's top-level files, or the files in its top-level folders, that the
    overlay does not show as a file; None when all do."""
    for entry in game.iterdir():
        if entry.is_file():
            if not (overlay / entry.name).is_file():
                return Path(entry.name)
        elif entry.is_dir():
            for inner in entry.iterdir():
                if inner.is_file() and not (overlay / entry.name / inner.name).is_file():
                    return Path(entry.name) / inner.name
    return None


def _mirror_directory(link, target):
    link.mkdir()
    for entry in target.iterdir():
        make_link(link / entry.name, entry)


def make_link(link, target):
    global MIRROR_DIRECTORIES
    if os.name != 'nt':
        link.symlink_to(target, target_is_directory=target.is_dir())
        return
    if target.is_dir():
        if MIRROR_DIRECTORIES:
            _mirror_directory(link, target)
            return
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            LINKED[str(link)] = target
        if _directory_link_followed(link, target):
            return
        remove_link(link)
        MIRROR_DIRECTORIES = True
        print('Mods: this process may not follow folder links (Windows redirection trust, or '
              'Wine/Proton); copying the folder structure with hard links instead', file=sys.stderr)
        _mirror_directory(link, target)
        return
    else:
        try:
            os.link(target, link)
        except OSError as error:
            if MIRROR_DIRECTORIES and MIRROR_GAME in target.parents:
                # Mirrored folders hold every game file: copies would be the whole game.
                raise UnresolvedLinks(f'no hard link to the game\'s {target.name} ({error})') from error
            shutil.copy2(target, link)
    LINKED[str(link)] = target


def is_link(path):
    return path.is_symlink() or str(path) in LINKED or (hasattr(path, 'is_junction') and path.is_junction())


def link_target(path):
    return LINKED.get(str(path)) or path.resolve(strict=True)


def remove_link(path):
    if hasattr(path, 'is_junction') and path.is_junction():
        os.rmdir(path)
    else:
        unlink_file(path, LINKED.get(str(path)))
    LINKED.pop(str(path), None)


def unlink_file(path, linked=None):
    """Windows refuses to delete a read-only file (WinError 5), and a hard link shares the
    attribute with the file it links: game files copied from a read-only source keep it. It is
    cleared for the deletion and set again on `linked`, the file that stays. From emurphy04
    (Supermedo/bloodborne_pc PR #77, ba34a3a)."""
    try:
        path.unlink()
        return
    except PermissionError:
        mode = path.stat().st_mode
        if os.name != 'nt' or mode & stat.S_IWRITE:
            raise
    os.chmod(path, mode | stat.S_IWRITE)
    try:
        path.unlink()
    finally:
        rest = path if path.exists() else linked
        if rest is not None and rest.exists():
            os.chmod(rest, mode)


def remove_overlay(overlay, game=None):
    """shutil.rmtree for an overlay, whose read-only links are deleted as remove_link does
    (`game`: the game folder it mirrors, for links made by another run)."""
    def retry(function, path, error):
        path = Path(path)
        if function not in (os.unlink, os.remove) or not path.is_file():
            raise error[1] if isinstance(error, tuple) else error
        linked = LINKED.get(str(path))
        if linked is None and game is not None:
            mirror = Path(game) / path.relative_to(overlay)
            if mirror.is_file() and os.path.samefile(path, mirror):
                linked = mirror
        unlink_file(path, linked)
    if sys.version_info >= (3, 12):
        shutil.rmtree(overlay, onexc=retry)
    else:
        shutil.rmtree(overlay, onerror=retry)


def expand(directory):
    """Materialize one directory level; never write through a directory link."""
    if is_link(directory):
        target = link_target(directory)
        if not target.is_dir():
            raise ValueError(f'File/directory conflict at {directory.name}')
        remove_link(directory)
        directory.mkdir()
        for entry in target.iterdir():
            make_link(directory / entry.name, entry)
    elif directory.exists() and not directory.is_dir():
        raise ValueError(f'File/directory conflict at {directory.name}')
    else:
        directory.mkdir(exist_ok=True)


def build_overlay(game, out, mods):
    game = Path(game).resolve(strict=True)
    replacements = []
    owners = {}
    for name, root in mods:
        count = 0
        for relative, source in mod_files(root):
            key = relative.as_posix().casefold()
            if key in owners:
                print(f'Mods: {relative}: {owners[key]} -> {name}', file=sys.stderr)
            owners[key] = name
            replacements.append((relative, source))
            count += 1
        print(f'Mods: {name}: {count} files', file=sys.stderr)
    if not replacements:
        return game
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    places = [out]
    if os.name == 'nt':
        if out.drive.casefold() != game.drive.casefold():
            # Hard links only reach files on the same volume: the overlay goes beside the game.
            places.insert(0, game.parent)
        # A drive without links (exFAT, FAT32, some network shares: "Incorrect function") cannot
        # hold the overlay; folder links on another drive still reach the game, and its files are
        # copied (only the folders a mod changes are opened up).
        temp = Path(tempfile.gettempdir()).resolve()
        if temp not in places:
            places.append(temp)
    for at, place in enumerate(places):
        try:
            return _checked_overlay_at(game, place, replacements)
        except OSError as error:
            if at == len(places) - 1:
                if isinstance(error, UnresolvedLinks):
                    # Better a start without mods (and the port's own generated files) than none.
                    print(f'Mods: {error}; starting without mods. Report this with last_run.log.',
                          file=sys.stderr)
                    return game
                raise
            print(f'Mods: no overlay in {place} ({error}); trying {places[at + 1]}', file=sys.stderr)


def _checked_overlay_at(game, out, replacements):
    """_overlay_at, checked: when the game's files do not open through its links (Wine/Proton,
    GitHub issue #42), again with real folders of hard-linked or copied files; UnresolvedLinks
    when they still do not."""
    for mirror in (False, True):
        overlay = _overlay_at(game, out, replacements, mirror)
        missing = _unresolved(overlay, game)
        if missing is None:
            return overlay
        remove_overlay(overlay, game)
        if mirror or MIRROR_DIRECTORIES:
            break
        print(f'Mods: {missing} does not open through the overlay\'s folder links; copying the '
              'folder structure with hard links instead', file=sys.stderr)
    raise UnresolvedLinks(f'the overlay in {out} does not show the game\'s {missing}')


def _overlay_at(game, out, replacements, mirror=False):
    global MIRROR_DIRECTORIES, MIRROR_GAME
    MIRROR_DIRECTORIES = mirror
    MIRROR_GAME = game
    LINKED.clear()
    overlay = Path(tempfile.mkdtemp(prefix='mod-game-', dir=out))
    try:
        for entry in game.iterdir():
            make_link(overlay / entry.name, entry)
        replaced = added = 0
        for relative, source in replacements:
            # Each component takes the game's spelling when it exists in another case.
            parent = overlay
            for part in relative.parts[:-1]:
                parent = child(parent, part)
                expand(parent)
            destination = child(parent, relative.parts[-1])
            if is_link(destination):
                replaced += link_target(destination).is_relative_to(game)
                remove_link(destination)
            elif destination.exists():
                raise ValueError(f'File/directory conflict: {relative}')
            else:
                added += 1
            make_link(destination, source)
        print(f'Mods: {replaced} game files replaced, {added} added', file=sys.stderr)
        return overlay
    except BaseException:
        remove_overlay(overlay, game)
        raise


# The in-game PC menu's layer (enhancements.py) adds its strings to every language's menu text.
# A layer above it with its own copy of that file (a mod that edits menu text, such as Restored
# Change Appearance Feature, or the Brazilian Portuguese text layer) hid them, and the PC rows
# showed message ids instead of names (GitHub issue #48). The strings are added to that copy
# instead, in a layer on top of the others.
PC_MENU_LAYER = 'pc-menu'
PC_MENU_TEXT_LAYER = 'pc-menu-text'
PC_MENU_TEXT_FILE = 'menu.msgbnd.dcx'
PC_MENU_MOVIES = {'optionsetting.gfx', 'title.gfx', 'title_dlc.gfx'}


def pc_menu_text_identity():
    """What the merged files depend on besides their sources: the menu's scripts and strings."""
    here = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ('menu_assets.py', 'fromsoft.py', 'pc_menu_messages.tsv'):
        try:
            digest.update((here / name).read_bytes())
        except OSError:
            pass
    return f'pc-menu-text 1 {digest.hexdigest()[:16]}'


def keep_pc_menu_text(layers, generated, out):
    """`layers`, with one more on top when a layer above the PC menu's replaces a language's
    menu text: that copy with the PC menu's strings added. A mod replacing one of the movies the
    menu edits is only reported (its sections would have to be rebuilt in the mod's movie)."""
    names = [name for name, _root in layers]
    if PC_MENU_LAYER not in names:
        return layers
    owners = {}  # game path, case-folded -> (game path, file, layer): the last layer wins
    for name, root in layers[names.index(PC_MENU_LAYER) + 1:]:
        for relative, source in mod_files(root):
            parts = [p.casefold() for p in relative.parts]
            if len(parts) == 4 and parts[1] == 'msg' and parts[3] == PC_MENU_TEXT_FILE:
                owners[relative.as_posix().casefold()] = (relative, source, name)
            elif len(parts) == 3 and parts[1] == 'menu' and parts[2] in PC_MENU_MOVIES and \
                    name not in generated:
                print(f'Mods: {name} replaces {relative.as_posix()}, which the in-game PC menu also '
                      'edits; the mod\'s copy is used and the PC menu may be incomplete',
                      file=sys.stderr)
    if not owners:
        return layers
    folder = Path(out) / PC_MENU_TEXT_LAYER
    stamp_file = folder / 'stamp.txt'
    lines = [pc_menu_text_identity()]
    for key in sorted(owners):
        relative, source, _name = owners[key]
        info = source.stat()
        lines.append(f'{relative.as_posix()}\t{source}\t{info.st_size}\t{int(info.st_mtime)}')
    stamp = '\n'.join(lines) + '\n'
    try:
        previous = stamp_file.read_text(encoding='utf-8')
    except OSError:
        previous = None
    made = [line.split('\t', 1)[0] for line in (previous or '').splitlines()[1:]]
    if previous == stamp and made and all((folder / m).is_file() for m in made):
        return [*layers, (PC_MENU_TEXT_LAYER, folder)]
    # Only the files this function wrote go, one by one: nothing here is a link into the game.
    for m in made:
        old = folder / m
        if old.is_file() and not is_link(old):
            old.unlink()
    if stamp_file.is_file():
        stamp_file.unlink()
    import menu_assets
    rows = menu_assets.read_messages()
    lines = lines[:1]
    for key in sorted(owners):
        relative, source, name = owners[key]
        try:
            data = menu_assets.build_menu_messages(source.read_bytes(), rows)
        except (OSError, menu_assets.FormatError, IndexError, struct.error) as error:
            # One that already has the menu's ids was made from the PC menu's own file and works.
            print(f'Mods: {name}\'s {relative.as_posix()} is used as it is ({error}); the PC '
                  'menu\'s rows may show numbers in that language', file=sys.stderr)
            continue
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        info = source.stat()
        lines.append(f'{relative.as_posix()}\t{source}\t{info.st_size}\t{int(info.st_mtime)}')
        print(f'Mods: PC menu strings added to {name}\'s {relative.as_posix()}', file=sys.stderr)
    if len(lines) == 1:
        return layers
    # A file left as it is keeps the stamp from matching, so it is tried (and reported) again.
    stamp_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return [*layers, (PC_MENU_TEXT_LAYER, folder)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--mods-dir', required=True, type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--enabled', choices=('0', '1'), default='1')
    # Files the port's own features make from the game (PC enhancements, menu assets), each a
    # folder with a dvdroot_ps4 layout. They lie under the player's mods, which still win, and
    # apply with mods off: every feature has its own switch.
    parser.add_argument('--generated', type=Path, action='append', default=[])
    args = parser.parse_args()
    layers = [(folder.name, folder) for folder in args.generated if content_root(folder)]
    if args.enabled == '1':
        # shadPS4's loose overlay convention, beside the original game.
        # Another launcher's overlay there would stop every start, though the player never chose
        # it here: BBLauncher builds it from symlinks into its own Mods-Active folder, or leaves
        # it empty. Such a folder is left out, with what to do.
        legacy = Path(str(args.game.resolve()) + '-mods')
        if legacy.is_dir():
            try:
                for _entry in mod_files(legacy):  # what build_overlay would refuse
                    pass
            except ValueError as error:
                why = ('its files are symlinks (made by another launcher, such as BBLauncher)'
                       if 'symlink' in str(error) else str(error))
                print(f'Mods: {legacy} left out: {why}. Copy the mod itself into the Mods folder '
                      f'to use it here.', file=sys.stderr)
            else:
                layers.append((legacy.name, legacy))
        # A directly selected loose overlay is also accepted.
        if child(args.mods_dir, 'dvdroot_ps4').is_dir():
            layers.append((args.mods_dir.name, args.mods_dir))
        else:
            for name in selected(args.mods_dir, args.config):
                layers.append((name, args.mods_dir / name))
    generated = {folder.name for folder in args.generated}
    try:
        layers = keep_pc_menu_text(layers, generated, args.out)
    except (OSError, ValueError) as error:
        print(f'Mods: PC menu strings not merged ({error})', file=sys.stderr)
    shutil.rmtree(args.out / 'script_edits', ignore_errors=True)  # an earlier start's
    if args.enabled == '1':
        layers += script_edits_layer(args.game, layers, generated, args.out / 'script_edits')
    print(build_overlay(args.game, args.out, layers))


def resolved(game, layers):
    """A function: game path (dvdroot_ps4/...) -> its bytes as the overlay of `layers` would give
    them (the last layer that has the file, else the game's), or None if there is no such file."""
    owners = {}
    for _name, root in layers:
        for relative, source in mod_files(root):
            owners[relative.as_posix().casefold()] = source

    def read(relative):
        source = owners.get(Path(relative).as_posix().casefold())
        if source is None:
            source = Path(game)
            for part in Path(relative).parts:
                source = child(source, part)
        return source.read_bytes() if source.is_file() else None
    return read


def script_edits_layer(game, layers, generated, out):
    """[(name, folder)]: the layer of the mods' event edits (scripts/script_edits.py), applied in
    mod order on top of what `layers` give, or [] if no edit applied. Never stops the start."""
    edited = [(name, root) for name, root in layers if name not in generated and has_edits(root)]
    if not edited:
        return []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import script_edits
        layer = script_edits.build(resolved(game, layers), edited, out,
                                   log=lambda line: print(line, file=sys.stderr))
    except Exception as error:  # noqa: BLE001 - never stops the start (port-a2866f9-F1);
        # ImportError: a Python without tomllib
        print(f'Script edits: none applied ({type(error).__name__}: {error})', file=sys.stderr)
        return []
    return [('script_edits', layer)] if layer else []


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TypeError) as error:
        print(f'Mods: {error}', file=sys.stderr)
        sys.exit(1)
