# SPDX-License-Identifier: GPL-3.0-or-later
"""Plugins and Lua scripts for the launcher's Mods page, read without running any of them.

- A plugin's name, version, description, flags and settings come from its DLL's exported data
  (bb_plugin_info, bb_plugin_options), read from the file: the DLL is never loaded, so nothing in it
  runs in the launcher.
- Which plugins the game would load, and why it would leave any out, follows src/plugin_host.c
  (scan, load_one): the same folders, the same order, the same checks.
- plugins.ini's [plugins] switches are changed in place: one line's value, everything else in the
  file kept as it was.
- The last run's log says what really happened (a DLL Windows can't load, a plugin whose start
  failed), which a file alone can't tell.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import struct
import tempfile

API_VERSION = 16           # BB_PLUGIN_API_VERSION of the host this launcher ships with
OPT_IN, GAMEPLAY, ADOPTS_RULES = 1, 2, 4   # BB_PLUGIN_* flags
OPTION_TYPES = {1: 'bool', 2: 'int', 3: 'float', 4: 'choice', 5: 'text', 6: 'action', 7: 'heading'}
OPTION_LIVE = 0x100
NAME_RE = re.compile(r'[A-Za-z0-9_-]{1,63}')
MAX_OPTIONS = 256          # a table that doesn't end by then is taken as broken
MAX_TEXT = 4096            # the longest string read out of a DLL
MAX_EXPORTS = 16384        # a DLL exporting more names is taken as broken (bounds the work a file can cause)
MAX_PLUGINS = 64           # the most plugins the host loads (plugin_host.c MAX_PLUGINS)


class PluginFileError(Exception):
    """The file isn't a 64-bit Windows DLL this reader can make sense of."""


class _Pe:
    """The parts of a PE32+ file needed to read exported data: sections and the export table."""

    def __init__(self, data):
        self.data = data
        if data[:2] != b'MZ' or len(data) < 0x40:
            raise PluginFileError('not a Windows DLL')
        pe = self._u32(0x3C)
        if data[pe:pe + 4] != b'PE\0\0':
            raise PluginFileError('not a Windows DLL')
        machine, sections, _, _, _, optional_size, _ = struct.unpack('<HHIIIHH', self._bytes(pe + 4, 20))
        if machine != 0x8664:
            raise PluginFileError('not a 64-bit (x64) DLL')
        optional = pe + 24
        if self._u16(optional) != 0x20B:
            raise PluginFileError('not a 64-bit (PE32+) DLL')
        self.image_base = self._u64(optional + 24)
        directories = self._u32(optional + 108)
        export_rva = self._u32(optional + 112) if directories >= 1 else 0
        self.sections = []
        table = optional + optional_size
        for i in range(min(sections, 96)):
            at = table + 40 * i
            virtual_size, virtual_address, raw_size, raw_at = struct.unpack_from('<IIII', self._bytes(at + 8, 16))
            self.sections.append((virtual_address, max(virtual_size, raw_size), raw_at, raw_size))
        self.exports = self._exports(export_rva) if export_rva else {}

    def _bytes(self, at, n):
        if at < 0 or at + n > len(self.data):
            raise PluginFileError('the file is cut short')
        return self.data[at:at + n]

    def _u16(self, at):
        return struct.unpack('<H', self._bytes(at, 2))[0]

    def _u32(self, at):
        return struct.unpack('<I', self._bytes(at, 4))[0]

    def _u64(self, at):
        return struct.unpack('<Q', self._bytes(at, 8))[0]

    def read(self, rva, n):
        """n bytes at an address in the image: from the file, or zeros past a section's file data."""
        for virtual_address, size, raw_at, raw_size in self.sections:
            if virtual_address <= rva and rva + n <= virtual_address + size:
                offset = rva - virtual_address
                stored = self._bytes(raw_at + offset, max(0, min(n, raw_size - offset))) if offset < raw_size else b''
                return stored + bytes(n - len(stored))
        raise PluginFileError(f'address {rva:#x} is outside the image')

    def text_at_rva(self, rva):
        """The NUL-ended string at an address, at most MAX_TEXT bytes. Past a section's file data
        the image holds zeros, so a string running into them ends there."""
        for virtual_address, size, raw_at, raw_size in self.sections:
            if virtual_address <= rva < virtual_address + size:
                offset = rva - virtual_address
                if offset >= raw_size:
                    return ''
                start = raw_at + offset
                stop = min(raw_at + raw_size, start + MAX_TEXT + 1, len(self.data))
                end = self.data.find(b'\0', start, stop)
                if end < 0:
                    if stop - start > MAX_TEXT:
                        raise PluginFileError('a string in the DLL has no end')
                    if stop == len(self.data) and stop < raw_at + raw_size:
                        raise PluginFileError('the file is cut short')
                    end = stop  # the section's file data ends: zeros follow
                return self.data[start:end].decode('utf-8', 'replace')
        raise PluginFileError(f'address {rva:#x} is outside the image')

    def text_at(self, va):
        """The string a pointer in the image's data points to ('' for NULL)."""
        if not va:
            return ''
        if va < self.image_base:
            raise PluginFileError(f'a pointer ({va:#x}) is outside the image')
        return self.text_at_rva(va - self.image_base)

    def _exports(self, rva):
        directory = self.read(rva, 40)
        names, functions_at, names_at, ordinals_at = struct.unpack_from('<IIII', directory, 24)
        functions = struct.unpack_from('<I', directory, 20)[0]
        out = {}
        if names > MAX_EXPORTS:
            raise PluginFileError(f'{names} exported names: more than a plugin has')
        for i in range(names):
            name = self.text_at_rva(struct.unpack('<I', self.read(names_at + 4 * i, 4))[0])
            ordinal = struct.unpack('<H', self.read(ordinals_at + 2 * i, 2))[0]
            if ordinal < functions:
                out[name] = struct.unpack('<I', self.read(functions_at + 4 * ordinal, 4))[0]
        return out


@dataclass
class Plugin:
    file: str                      # the DLL's file name: a later folder's file of that name replaces it
    path: Path
    name: str = ''                 # bb_plugin_info's name, else the file's stem: plugins.ini's [plugins] key
    title: str = ''
    version: str = ''
    author: str = ''
    description: str = ''
    api_version: int = 0           # 0: no bb_plugin_info
    flags: int = 0
    options: list = field(default_factory=list)
    problem: str = ''              # why the game can't load it whatever the switches say
    switch: int = -1               # plugins.ini [plugins] <name>: 1 on, 0 off, -1 not said
    loads: bool = False            # the game would load it, as things are now
    why: str = ''                  # why not, in the host's words

    @property
    def gameplay(self):
        return bool(self.flags & GAMEPLAY)

    @property
    def opt_in(self):
        return bool(self.flags & OPT_IN)

    @property
    def enabled(self):
        """What the Mods page's switch shows: the player's choice, or the plugin's default."""
        return self.switch == 1 or (self.switch == -1 and not self.opt_in)


def read_plugin(path, data=None):
    """A Plugin with what the DLL's file says about itself; problem is set when it isn't one.
    data: the file's bytes, when the caller has them already (else they're read from path)."""
    path = Path(path)
    plugin = Plugin(file=path.name, path=path, name=path.stem)
    try:
        pe = _Pe(path.read_bytes() if data is None else data)
        if 'bb_plugin_init' not in pe.exports and 'bb_plugin_image' not in pe.exports:
            plugin.problem = 'exports neither bb_plugin_init nor bb_plugin_image: not a plugin'
            return plugin
        info = pe.exports.get('bb_plugin_info')
        if info is not None:
            api, flags, *pointers = struct.unpack('<II5Q', pe.read(info, 48))
            name, plugin.title, plugin.version, plugin.author, plugin.description = (pe.text_at(p) for p in pointers)
            plugin.api_version, plugin.flags = api, flags
            if name:
                plugin.name = name
        table = pe.exports.get('bb_plugin_options')
        if table is not None:
            plugin.options = _options(pe, table)
    except (OSError, PluginFileError) as error:
        plugin.problem = f"can't be read: {error}"
        return plugin
    if not NAME_RE.fullmatch(plugin.name):
        plugin.problem = "its name must be 1-63 letters, digits, '_' or '-'"
    elif plugin.api_version > API_VERSION:
        plugin.problem = f"built for a newer plugin API than this host's ({plugin.api_version} > {API_VERSION})"
    return plugin


def _options(pe, rva):
    """bb_plugin_options: its entries up to the zero one (struct BbOption, 64 bytes each)."""
    out = []
    for i in range(MAX_OPTIONS):
        kind, key, label, help_, default, low, high, choices = struct.unpack(
            '<I4xQQQQddQ', pe.read(rva + 64 * i, 64))
        if kind == 0:
            return out
        out.append({'type': OPTION_TYPES.get(kind & 0xFF, 'unknown'), 'live': bool(kind & OPTION_LIVE),
                    'key': pe.text_at(key), 'label': pe.text_at(label), 'help': pe.text_at(help_),
                    'default': pe.text_at(default), 'min': low, 'max': high,
                    'choices': [c for c in pe.text_at(choices).split('|') if c] if choices else []})
    raise PluginFileError('bb_plugin_options has no end')


def plugin_dirs(data_dir, user_dir):
    """The folders the game loads plugins from, in order: a later one's file replaces an earlier one's."""
    return [Path(data_dir) / 'plugins', Path(user_dir) / 'plugins']


def scan(data_dir, user_dir, ini=None, online=False, all_off=False):
    """Every plugin the game would find, in load order, with whether it would load and why not.

    online: online play without seamless co-op (BB_ONLINE=1, BB_COOP unset), which leaves out the
    plugins flagged as changing the game. all_off: BB_PLUGINS=0, which loads none."""
    found = {}
    for folder in plugin_dirs(data_dir, user_dir):
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() == '.dll' and entry.is_file():
                found[entry.name.lower()] = entry
    ini = ini if ini is not None else PluginsIni(Path(user_dir) / 'plugins.ini')
    plugins, loaded = [], set()
    for key in sorted(found):
        plugin = read_plugin(found[key])
        plugin.switch = ini.switch(plugin.name)
        if all_off:
            why = 'no plugin loads (BB_PLUGINS=0)'
        elif len(loaded) == MAX_PLUGINS:  # the host's first check, before it reads the file
            why = f'{MAX_PLUGINS} plugins are the most this host loads'
        else:
            why = plugin.problem
        if not why and plugin.name.lower() in loaded:
            why = 'another plugin has its name'
        if not why:
            if plugin.switch == 0:
                why = 'off in plugins.ini'
            elif plugin.switch < 0 and plugin.opt_in:
                why = 'off until turned on in plugins.ini (opt-in)'
            elif plugin.gameplay and online:
                why = 'changes the game, and this is online play'
        plugin.loads, plugin.why = not why, why
        if plugin.loads:
            loaded.add(plugin.name.lower())
        plugins.append(plugin)
    return plugins


class PluginsIni:
    """<user>\\plugins.ini, read as the host reads it and changed one value at a time."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raw = b''
        self.bom = raw.startswith(b'\xef\xbb\xbf')
        text = raw[3:].decode('utf-8', 'replace') if self.bom else raw.decode('utf-8', 'replace')
        self.newline = '\r\n' if '\r\n' in text else '\n'
        self.lines = text.splitlines(keepends=True)

    @staticmethod
    def _parse(line):
        """(section or None, key, value, value's span) as the host reads one line; None to skip."""
        s = line.strip(' \t\r\n')
        if not s or s[0] in '#;':
            return None
        if s[0] == '[':
            close = s.find(']')
            return (s[1:close].strip(' \t'), None, None, None) if close >= 0 else None
        eq = line.find('=')
        if eq < 0:
            return None
        end = len(line.rstrip('\r\n'))
        for at in range(eq + 2, end):  # a comment: ; or # after a space or tab
            if line[at] in ';#' and line[at - 1] in ' \t':
                end = at
                break
        value = line[eq + 1:end]
        start = eq + 1 + (len(value) - len(value.lstrip(' \t')))
        stop = eq + 1 + len(value.rstrip(' \t'))
        return None, line[:eq].strip(' \t﻿'), value.strip(' \t'), (start, max(start, stop))

    def _entries(self):
        """(line index, section, key, value, span) for every key = value line inside a section."""
        section = ''
        for index, line in enumerate(self.lines):
            parsed = self._parse(line)
            if not parsed:
                continue
            if parsed[0] is not None:
                section = parsed[0]
            elif section:
                yield index, section, parsed[1], parsed[2], parsed[3]

    def get(self, section, key):
        found = None
        for _, s, k, value, _ in self._entries():
            if s.lower() == section.lower() and k.lower() == key.lower():
                found = value  # the last line wins
        return found

    def switch(self, name, section='plugins'):
        value = self.get(section, name)
        if value is None:
            return -1
        value = value.lower()
        return 1 if value in ('1', 'true', 'on') else 0 if value in ('0', 'false', 'off') else -1

    def set_switch(self, name, on, section='plugins'):
        """[section] name = 1 or 0: the line the host reads (the last) gets the new value; with none,
        a line is added at the end of the last [section], or a new [section] at the file's end."""
        value = '1' if on else '0'
        last = section_end = None   # the line to change; the target section's last non-blank line
        current = ''
        for index, line in enumerate(self.lines):
            parsed = self._parse(line)
            if parsed and parsed[0] is not None:
                current = parsed[0]
            inside = current.lower() == section.lower()
            if inside and line.strip(' \t\r\n'):
                section_end = index
            if inside and parsed and parsed[0] is None and parsed[1].lower() == name.lower():
                last = (index, parsed[3])
        if last:
            index, (start, stop) = last
            line = self.lines[index]
            if start == stop and line[start - 1] == '=':
                value = ' ' + value  # "name =" with no value: "name = 1"
            self.lines[index] = line[:start] + value + line[stop:]
            return
        if self.lines and not self.lines[-1].endswith(('\n', '\r')):
            self.lines[-1] += self.newline
        if section_end is not None:
            self.lines.insert(section_end + 1, f'{name} = {value}{self.newline}')
        else:
            if self.lines and self.lines[-1].strip():
                self.lines.append(self.newline)
            self.lines += [f'[{section}]{self.newline}', f'{name} = {value}{self.newline}']

    def text(self):
        return ''.join(self.lines)

    def save(self):
        """Written whole to a temporary file first, then put in place, so the game never reads half."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = (b'\xef\xbb\xbf' if self.bom else b'') + self.text().encode('utf-8')
        handle, temporary = tempfile.mkstemp(prefix='plugins.ini.', dir=self.path.parent)
        try:
            with os.fdopen(handle, 'wb') as out:
                out.write(data)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


_LOADED = re.compile(r'^(?P<name>[A-Za-z0-9_-]{1,63}) (?:(?P<version>\S+) )?\((?P<file>.+)\)$')
_LEFT_OUT = re.compile(r'^(?P<name>.+?) \((?P<file>.+)\): left out, (?P<why>.+)$')
_UNLOADED = re.compile(r'^(?P<name>[A-Za-z0-9_-]{1,63}): (?P<why>.+); unloaded$')
_NO_LOAD = re.compile(r'^(?P<file>.+): could not be loaded \((?P<why>.+)\)$')
_CAPPED = re.compile(r'^(?P<file>.+): left out, (?P<why>\d+ plugins are the most this host loads)$')
_SCRIPT = re.compile(r'^Plugin lua: (?P<script>[^:]+): (?:loaded \(|stopped: (?P<why>.+)$)')


def last_run(log_text):
    """What the log of one game run says happened to each plugin DLL and Lua script:
    {'plugins': {file name: {'name': ..., 'state': 'loaded'|'left out'|'unloaded'|'failed', 'why': ...}},
     'scripts': {script: {'state': 'running'|'stopped', 'why': ...}}}.
    Keyed by the DLL's file name ('x.dll', as scan()'s Plugin.file), since two files may declare
    one name: the host loads the first and leaves out the other. An "unloaded" line names the
    plugin only; it's the file loaded under that name. 'name' is '' where the line gives none."""
    plugins, scripts, loaded_as = {}, {}, {}
    for line in log_text.splitlines():
        line = line.rstrip('\r')
        if line.startswith('Plugins: '):
            body = line[len('Plugins: '):]
            for pattern, state in ((_CAPPED, 'left out'), (_LEFT_OUT, 'left out'), (_UNLOADED, 'unloaded'),
                                   (_NO_LOAD, 'failed'), (_LOADED, 'loaded')):
                match = pattern.match(body)
                if not match:
                    continue
                groups = match.groupdict()
                name = groups.get('name') or ''
                if groups.get('file'):
                    file = Path(groups['file'].replace('\\', '/')).name
                else:
                    file = loaded_as.get(name.lower(), name)
                if state == 'loaded':
                    loaded_as[name.lower()] = file
                plugins[file] = {'name': name, 'state': state, 'why': groups.get('why') or ''}
                break
        else:
            match = _SCRIPT.match(line)
            if match:
                why = match.group('why')
                scripts[match.group('script')] = {'state': 'stopped' if why else 'running', 'why': why or ''}
    return {'plugins': plugins, 'scripts': scripts}


def lua_scripts(user_dir, mods=()):
    """The Lua scripts bb_lua would run, in its order: <user>\\plugins\\lua\\*.lua named by file, then
    each mod's scripts\\*.lua named <mod>/<file>. mods: (name, folder, enabled) in the player's order;
    a disabled mod's scripts are listed with enabled False."""
    out = []

    def folder(path, prefix, mod, enabled):
        try:
            files = sorted((f for f in Path(path).iterdir() if f.suffix.lower() == '.lua' and f.is_file()),
                           key=lambda f: f.name.lower())
        except OSError:
            return
        for f in files:
            out.append({'name': prefix + f.stem, 'path': str(f), 'mod': mod, 'enabled': enabled})

    folder(Path(user_dir) / 'plugins' / 'lua', '', '', True)
    for name, path, enabled in mods:
        folder(Path(path) / 'scripts', f'{name}/', name, bool(enabled))
    return out
