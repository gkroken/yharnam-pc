#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn events written in DarkScript3 into an event edit file (script_edits.py), at authoring time.

    python scripts/script_convert.py <map>.emevd.js --emedf <bb-common.emedf.json> --events 12415990,...
        --out <mod>/edits/event/<map>.emevd.edit.toml

<map>.emevd.js is DarkScript3's text of an event file (File > Save as JS, or its editor's text),
with the events you added; --events names them (the rest of the file is the game's and stays
out). The instruction table is the one your DarkScript3 has (Resources/bb-common.emedf.json): its
names and argument types, read here and never shipped. The edit file goes to --out (UTF-8, whatever
the shell would write), else standard output,
with every instruction raw (bank, id, typed arguments), so it needs neither DarkScript3 nor the
table to apply. An event is started from the map (run = true) when the file's event 0 has
InitializeEvent(0, <id>, 0) for it.

Plain instruction lists only: an event with parameters, or with MattScript (if/else, goto,
labels, const), is refused with why.

A compiled event file (Soulstruct's output, DarkScript3's saved .emevd.dcx, any tool's) needs no
table: --game <game folder> compares it with the game's file of the same name, and the events the
game's lacks (or --events) come out with their arguments as bytes (hex = "..."):

    python scripts/script_convert.py m24_01_00_00.emevd.dcx --game <game> --out <edit file>

sys.path gets this folder for emevd.py and fromsoft.py, which that needs.
"""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# EMEDF argument types: script_edits' type names.
TYPES = {0: 'u8', 1: 'u16', 2: 'u32', 3: 's8', 4: 's16', 5: 's32', 6: 'f32', 8: 'u32'}
RESTART = {'default': 'default', 'restart': 'restart', 'end': 'end'}


class ConvertError(ValueError):
    pass


def _key(name):
    """A name compared without case, spaces or punctuation: "Set Event Flag" = SetEventFlag."""
    return re.sub(r'[^0-9a-z]', '', name.casefold())


def load_emedf(text):
    """{key: (bank, id, name, [(type, {value key: number} or None, default or None)])} from an
    EMEDF JSON (which may have // comment lines)."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith('//')]
    data = json.loads('\n'.join(lines))
    enums = {e['name']: {_key(v): int(k) for k, v in e['values'].items()} for e in data.get('enums', [])}
    table, twice = {}, set()
    for bank in data['main_classes']:
        for instr in bank['instrs']:
            args = []
            for arg in instr['args']:
                if arg['type'] not in TYPES:
                    raise ConvertError(f'{instr["name"]}: argument type {arg["type"]} is not known')
                args.append((TYPES[arg['type']], enums.get(arg.get('enum_name')), arg.get('default')))
            key = _key(instr['name'])
            if key in table:
                twice.add(key)
            table[key] = (bank['index'], instr['index'], instr['name'], args)
    for key in twice:
        del table[key]  # two instructions of one name: never guessed
    return table


def _strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


EVENT = re.compile(r'\$?Event\(\s*(-?\d+)\s*,\s*(\w+)\s*,\s*function\s*\(([^)]*)\)\s*\{')


def events(text):
    """{id: (rest behaviour, parameters, body)} of a DarkScript3 file."""
    text = _strip_comments(text)
    found = {}
    for match in EVENT.finditer(text):
        depth, at = 1, match.end()
        while depth and at < len(text):
            depth += {'{': 1, '}': -1}.get(text[at], 0)
            at += 1
        if depth:
            raise ConvertError(f'event {match.group(1)}: no closing brace')
        found[int(match.group(1))] = (match.group(2), match.group(3).strip(), text[match.end():at - 1])
    return found


# MattScript's own words (DarkScript3's structured view): if/else, goto, labels, constants.
MATTSCRIPT_WORDS = {'if', 'else', 'goto', 'const', 'var', 'let', 'while', 'for', 'return', 'function', 'EndEvent'}
CALL = re.compile(r'^([A-Za-z_]\w*)\s*\((.*)\)$', re.S)


def _number(text):
    try:
        return int(text, 0)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def instruction(statement, table):
    """(raw instruction for TOML, comment) of one DarkScript3 statement."""
    match = CALL.match(statement.strip())
    words = re.findall(r'[A-Za-z_]\w*', statement)
    if (not match or re.search(r'[{}:?=<>!&|]', statement)
            or (words and words[0] in MATTSCRIPT_WORDS)):
        raise ConvertError(f'"{statement.strip()[:60]}" is not a plain instruction (MattScript isn\'t converted)')
    name, inner = match.groups()
    found = table.get(_key(name))
    if not found:
        raise ConvertError(f'{name}: not an instruction of this EMEDF (or two have that name)')
    bank, code, full, layout = found
    parts = [p.strip() for p in inner.split(',')] if inner.strip() else []
    # Every argument must be written. DarkScript3's table gives every argument a default, so a
    # default doesn't mean it may be left out; filling one in would turn a forgotten flag into
    # flag 0 (port-f4dfb7e-F1).
    if len(parts) != len(layout):
        raise ConvertError(f'{name} ({full}) takes {len(layout)} arguments, not {len(parts)}: '
                           'write every one')
    args = []
    for part, (kind, enum, _default) in zip(parts, layout):
        value = _number(part)
        if value is None and enum is not None:
            value = enum.get(_key(part.rsplit('.', 1)[-1]))
        if value is None:
            raise ConvertError(f'{name}: {part!r} is not a number or one of its enum names')
        if kind != 'f32' and isinstance(value, float):
            raise ConvertError(f'{name}: {part!r} is not a whole number')
        args.append(f'{kind}:{value}')
    return {'bank': bank, 'id': code, 'args': args}, f'{name}({", ".join(parts)})'


def _toml_instruction(raw):
    args = ', '.join(f'"{a}"' for a in raw['args'])
    return f'{{ bank = {raw["bank"]}, id = {raw["id"]}, args = [{args}] }}'


def convert(text, table, wanted):
    """The edit file (TOML text) for events `wanted` of DarkScript3 text."""
    found = events(text)
    started = set()
    if 0 in found:
        for statement in found[0][2].split(';'):
            match = CALL.match(statement.strip())
            if match and _key(match.group(1)) == 'initializeevent':
                parts = [p.strip() for p in match.group(2).split(',')]
                if len(parts) == 3 and parts[0] == '0' and parts[2] == '0' and _number(parts[1]) is not None:
                    started.add(_number(parts[1]))
    out = ['# Made by script_convert.py from DarkScript3 text: instructions raw (bank, id, typed args).']
    for event_id in wanted:
        if event_id not in found:
            raise ConvertError(f'event {event_id} is not in the file')
        rest, params, body = found[event_id]
        if params:
            raise ConvertError(f'event {event_id} has parameters ({params}): not converted')
        if rest.casefold() not in RESTART:
            raise ConvertError(f'event {event_id}: rest behaviour {rest} is not Default, Restart or End')
        lines = []
        for statement in body.split(';'):
            if not statement.strip():
                continue
            try:
                raw, comment = instruction(statement, table)
            except ConvertError as error:
                raise ConvertError(f'event {event_id}: {error}') from None
            lines.append(f'    {_toml_instruction(raw)},  # {comment}')
        if not lines:
            raise ConvertError(f'event {event_id} has no instructions')
        out += ['', '[[event]]', f'id = {event_id}', f'restart = "{RESTART[rest.casefold()]}"',
                f'run = {"true" if event_id in started else "false"}', 'instructions = [', *lines, ']']
    return '\n'.join(out) + '\n'


def _emevd(data):
    """An Emevd of an event file's bytes, DCX-compressed or not."""
    from emevd import Emevd, EmevdError
    from fromsoft import FormatError, dcx_decompress
    try:
        return Emevd(dcx_decompress(data) if data[:4] == b'DCX\0' else data)
    except (EmevdError, FormatError) as error:
        raise ConvertError(f'not an event file this can read ({error})') from None


REST_NAMES = {0: 'default', 1: 'restart', 2: 'end'}


def convert_compiled(mine, game, wanted=None):
    """The edit file (TOML text) for the events of the event file `mine` (bytes, as Soulstruct,
    DarkScript3 or any tool compiles it) that the game's own `game` doesn't have: those in
    `wanted`, else all of them. No instruction table is needed: the arguments are copied as bytes."""
    import struct
    ours, theirs = _emevd(mine), _emevd(game)
    ids = [struct.unpack_from('<q', row)[0] for row in ours.events]
    rows = dict(zip(ids, ours.events))
    present = {struct.unpack_from('<q', row)[0] for row in theirs.events}
    if wanted is None:
        wanted = [i for i in ids if i not in present]
        if not wanted:
            raise ConvertError('the file has no event the game\'s file doesn\'t have')
    started = {struct.unpack_from('<iii', args)[1] for bank, code, args in
               (ours.event_instructions(0) if 0 in rows else [])
               if (bank, code) == (2000, 0) and len(args) == 12 and args[:4] == b'\0' * 4
               and args[8:] == b'\0' * 4}
    out = ['# Made by script_convert.py from a compiled event file: arguments as their bytes (hex).']
    for event_id in wanted:
        if event_id not in rows:
            raise ConvertError(f'event {event_id} is not in the file')
        if event_id in present:
            raise ConvertError(f'event {event_id} is in the game\'s file too: an edit only adds events')
        row = rows[event_id]
        params, rest = struct.unpack_from('<q', row, 24)[0], struct.unpack_from('<q', row, 40)[0]
        if params:
            raise ConvertError(f'event {event_id} has parameters: not converted')
        if rest not in REST_NAMES:
            raise ConvertError(f'event {event_id}: rest behaviour {rest} is not 0, 1 or 2')
        # An instruction's layer (its row's last field; none: -1, as 0xffffffff) isn't part of
        # (bank, id, args): converting one would drop it (port-dcb27fd-N1).
        count, first = struct.unpack_from('<qq', row, 8)
        for k, ins in enumerate(ours.instructions[first // 0x20:first // 0x20 + count]):
            if struct.unpack_from('<q', ins, 24)[0] not in (-1, 0xffffffff):
                raise ConvertError(f'event {event_id}: instruction {k + 1} uses layers: not converted')
        lines = [f'    {{ bank = {bank}, id = {code}, hex = "{args.hex()}" }},'
                 for bank, code, args in ours.event_instructions(event_id)]
        if not lines:
            raise ConvertError(f'event {event_id} has no instructions')
        out += ['', '[[event]]', f'id = {event_id}', f'restart = "{REST_NAMES[rest]}"',
                f'run = {"true" if event_id in started else "false"}', 'instructions = [', *lines, ']']
    return '\n'.join(out) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('script', type=Path,
                        help="DarkScript3's text of the event file (.js), or a compiled event file "
                             "(.emevd or .emevd.dcx, as Soulstruct or DarkScript3 saves it)")
    parser.add_argument('--emedf', type=Path, help="for .js: your DarkScript3's bb-common.emedf.json")
    parser.add_argument('--game', type=Path, help='for a compiled file: the game folder (with dvdroot_ps4), '
                        'whose event file of the same name it is compared with')
    parser.add_argument('--events', help='the ids of the events you added, comma-separated (for a '
                        'compiled file: default, every event the game\'s file lacks)')
    parser.add_argument('--out', type=Path, help='the edit file to write (UTF-8); else standard output')
    args = parser.parse_args(argv)
    try:
        wanted = [int(x) for x in args.events.split(',') if x.strip()] if args.events else None
        name = args.script.name
        if name.casefold().endswith(('.emevd', '.emevd.dcx')):
            if not args.game:
                raise ConvertError('a compiled event file needs --game, to compare it with the game\'s')
            stem = name[:name.casefold().index('.emevd')]
            game_file = args.game / 'dvdroot_ps4' / 'event' / f'{stem}.emevd.dcx'
            text = convert_compiled(args.script.read_bytes(), game_file.read_bytes(), wanted)
        else:
            if not args.emedf or not wanted:
                raise ConvertError('DarkScript3 text needs --emedf and --events')
            table = load_emedf(args.emedf.read_text(encoding='utf-8-sig'))
            text = convert(args.script.read_text(encoding='utf-8-sig'), table, wanted)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)  # a new mod's edits\event (tool-e0f3993-F1)
            args.out.write_text(text, encoding='utf-8', newline='\n')  # never the shell's encoding
        else:
            sys.stdout.write(text)
    except (ConvertError, ValueError, KeyError, OSError) as error:
        print(f'script_convert: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
