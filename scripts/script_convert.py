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
"""
import argparse
import json
from pathlib import Path
import re
import sys

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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('script', type=Path, help="DarkScript3's text of the event file (.js)")
    parser.add_argument('--emedf', type=Path, required=True, help="your DarkScript3's bb-common.emedf.json")
    parser.add_argument('--events', required=True, help='the ids of the events you added, comma-separated')
    parser.add_argument('--out', type=Path, help='the edit file to write (UTF-8); else standard output')
    args = parser.parse_args(argv)
    try:
        wanted = [int(x) for x in args.events.split(',') if x.strip()]
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
