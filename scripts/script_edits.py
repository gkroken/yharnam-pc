#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Event edits that ship with mods: data, never code, applied to the player's own files.

A mod adds events to an event file with `edits/event/<name>.emevd.edit.toml` in its folder,
which edits `event/<name>.emevd.dcx`:

    [[event]]
    id = 12405000                 # a new event: the file must not have it yet
    restart = "end"               # optional: what resting does to it: default, restart or end
    run = true                    # optional (true): InitializeEvent(0, id, 0) in event 0
    instructions = [
        "IfEventFlag(MAIN, ON, EventFlag, 12400000)",
        "SetEventFlag(12405001, ON)",
        { bank = 2003, id = 2, args = ["s32:12405002", "u8:1"] },
    ]

An instruction is a built-in name with its arguments (INSTRUCTIONS; an enum argument takes its
names, such as ON or MAIN), or raw: bank, id and typed arguments (u8, s8, u16, s16, u32, s32,
f32), packed as the game reads them (each aligned to its size, the whole to 4 bytes).

Every edit of every mod goes on top of the file the layers below give (the game's, or a mod's
replacement), in mod order, so two mods can add events to one file. One mod's edit file is
applied whole or not at all: one that fails (a bad name, an event id the file already has) is
left out with a line saying why, and the others still apply. The result is a generated layer
above the mods'. Nothing here runs a mod's code.
"""
from dataclasses import dataclass
from pathlib import Path
import struct
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emevd import Emevd, EmevdError  # noqa: E402
from fromsoft import FormatError, dcx_compress, dcx_decompress  # noqa: E402

EDITS_FOLDER = 'edits'
EVENT_SUFFIX = '.emevd.edit.toml'


class EditError(ValueError):
    pass


# Argument types: struct code and the range a value must be in.
TYPES = {
    'u8': ('B', 0, 0xff), 's8': ('b', -0x80, 0x7f),
    'u16': ('H', 0, 0xffff), 's16': ('h', -0x8000, 0x7fff),
    'u32': ('I', 0, 0xffffffff), 's32': ('i', -0x80000000, 0x7fffffff),
    'f32': ('f', None, None),
}

# Enum names by enum: the game's values (the community's Bloodborne instruction tables).
ON_OFF = {'OFF': 0, 'ON': 1}
ON_OFF_CHANGE = {'OFF': 0, 'ON': 1, 'CHANGE': 2}
CONDITION_GROUP = {'MAIN': 0, **{f'AND_{n:02}': n for n in range(1, 16)},
                   **{f'OR_{n:02}': -n for n in range(1, 16)}}
CONDITION_STATE = {'FAIL': 0, 'PASS': 1}
END_TYPE = {'End': 0, 'Restart': 1}
FLAG_TYPE = {'EventFlag': 0, 'EventID': 1, 'EventIDAndSlotNumber': 2}

# Built-in instructions: name -> (bank, id, ((type, enum or None), ...)). Each layout is the one
# the port's own event edits use (scripts/boss_rematches.py), which run in the game.
INSTRUCTIONS = {
    'IfConditionGroup': (0, 0, (('s8', CONDITION_GROUP), ('u8', CONDITION_STATE), ('s8', CONDITION_GROUP))),
    'IfEventFlag': (3, 0, (('s8', CONDITION_GROUP), ('u8', ON_OFF_CHANGE), ('u8', FLAG_TYPE), ('s32', None))),
    'EndUnconditionally': (1000, 4, (('u8', END_TYPE),)),
    'EndIfEventFlag': (1003, 2, (('u8', END_TYPE), ('u8', ON_OFF), ('u8', FLAG_TYPE), ('s32', None))),
    'SetEventFlag': (2003, 2, (('s32', None), ('u8', ON_OFF))),
    'AwardItemLot': (2003, 4, (('s32', None),)),
    'WarpPlayerToRespawnPoint': (2003, 49, (('s32', None),)),
}
# An event row's rest behaviour (what resting at a lamp does to it), after id, instruction
# count and offset, parameter count and offset.
RESTART = {'default': 0, 'restart': 1, 'end': 2}
REST_BEHAVIOUR = 40


def pack(bank, code, args):
    """(bank, id, bytes) of an instruction with [(type, value), ...] arguments."""
    data = bytearray()
    for kind, value in args:
        code_char, low, high = TYPES[kind]
        if kind == 'f32':
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EditError(f'{value!r} is not a number')
        elif isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise EditError(f'{value!r} is not a {kind}')
        size = struct.calcsize(code_char)
        data.extend(bytes(-len(data) % size))
        data.extend(struct.pack('<' + code_char, value))
    data.extend(bytes(-len(data) % 4))
    return bank, code, bytes(data)


def _number(text):
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        raise EditError(f'{text!r} is not a number') from None


def named(text):
    """An instruction written as Name(arg, ...)."""
    text = text.strip()
    name, open_paren, rest = text.partition('(')
    name = name.strip()
    if not open_paren or not rest.endswith(')') or not name.isidentifier():
        raise EditError(f'{text!r}: expected Name(argument, ...)')
    if name not in INSTRUCTIONS:
        raise EditError(f'{name}: not a built-in instruction (write it raw: bank, id and args)')
    bank, code, layout = INSTRUCTIONS[name]
    inner = rest[:-1].strip()
    parts = [p.strip() for p in inner.split(',')] if inner else []
    if len(parts) != len(layout):
        raise EditError(f'{name} takes {len(layout)} arguments, not {len(parts)}')
    args = []
    for part, (kind, enum) in zip(parts, layout):
        if enum is not None and part in enum:
            args.append((kind, enum[part]))
        elif part[:1].isalpha() or part[:1] == '_':
            names = ', '.join(enum) if enum else 'a number'
            raise EditError(f'{name}: {part!r} is not one of {names}')
        else:
            args.append((kind, _number(part)))
    return pack(bank, code, args)


def raw(table):
    unknown = set(table) - {'bank', 'id', 'args'}
    if unknown or not {'bank', 'id'} <= set(table):
        raise EditError(f'a raw instruction has bank, id and args, not {sorted(table)}')
    bank, code, args = table['bank'], table['id'], table.get('args', [])
    for value in (bank, code):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0x7fffffff:
            raise EditError(f'bank and id must be numbers from 0, not {value!r}')
    if not isinstance(args, list):
        raise EditError('args must be a list such as ["s32:1234", "u8:1"]')
    typed = []
    for arg in args:
        kind, colon, value = arg.partition(':') if isinstance(arg, str) else ('', '', '')
        if not colon or kind not in TYPES:
            raise EditError(f'{arg!r}: an argument is "<type>:<value>", type one of {", ".join(TYPES)}')
        typed.append((kind, _number(value.strip())))
    return pack(bank, code, typed)


@dataclass
class NewEvent:
    id: int
    instructions: list
    restart: int = 0
    run: bool = True


def parse(text):
    """The events of one edit file's TOML text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise EditError(f'not valid TOML ({error})') from None
    if set(data) - {'event'}:
        raise EditError(f'unknown keys {sorted(set(data) - {"event"})}: an edit file has [[event]] tables')
    events = data.get('event', [])
    if not isinstance(events, list) or not events:
        raise EditError('no [[event]] tables')
    result = []
    for number, event in enumerate(events, 1):
        where = f'event {number}'
        unknown = set(event) - {'id', 'restart', 'run', 'instructions'}
        if unknown:
            raise EditError(f'{where}: unknown keys {sorted(unknown)}')
        event_id = event.get('id')
        if isinstance(event_id, bool) or not isinstance(event_id, int) or not 0 < event_id <= 0x7fffffff:
            raise EditError(f'{where}: id must be a number above 0')
        where = f'event {event_id}'
        restart = event.get('restart', 'default')
        if restart not in RESTART:
            raise EditError(f'{where}: restart is one of {", ".join(RESTART)}')
        run = event.get('run', True)
        if not isinstance(run, bool):
            raise EditError(f'{where}: run is true or false')
        rows = event.get('instructions')
        if not isinstance(rows, list) or not rows:
            raise EditError(f'{where}: instructions must be a list with at least one')
        instructions = []
        for line, row in enumerate(rows, 1):
            try:
                if isinstance(row, str):
                    instructions.append(named(row))
                elif isinstance(row, dict):
                    instructions.append(raw(row))
                else:
                    raise EditError('an instruction is "Name(args)" or {bank, id, args}')
            except EditError as error:
                raise EditError(f'{where}, instruction {line}: {error}') from None
        result.append(NewEvent(event_id, instructions, RESTART[restart], run))
    ids = [e.id for e in result]
    if len(set(ids)) != len(ids):
        raise EditError('one event id is added twice')
    return result


def apply(data, events):
    """The EMEVD bytes `data` with `events` added (bytes in, bytes out, so a failure changes
    nothing)."""
    try:
        script = Emevd(data)
        present = {struct.unpack_from('<q', row)[0] for row in script.events}
        for event in events:
            if event.id in present:
                raise EditError(f'event {event.id} is already in the file')
            if event.run:
                script.append_initialized_event(event.id, event.instructions)
            else:
                script.append_event(event.id, event.instructions)
            struct.pack_into('<q', script.events[-1], REST_BEHAVIOUR, event.restart)
        return script.to_bytes()
    except EmevdError as error:
        raise EditError(str(error)) from None


def _child(folder, name):
    """`name` in `folder`, matched case-insensitively, as mods.child does."""
    for entry in (folder.iterdir() if folder.is_dir() else ()):
        if entry.name.casefold() == name.casefold():
            return entry
    return folder / name


def edit_files(mod):
    """(game path, edit file) of a mod's event edits: <mod>/edits/event/<name>.emevd.edit.toml."""
    folder = _child(_child(Path(mod), EDITS_FOLDER), 'event')
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if path.is_file() and path.name.casefold().endswith(EVENT_SUFFIX):
            stem = path.name[:-len(EVENT_SUFFIX)]
            found.append((Path('dvdroot_ps4') / 'event' / f'{stem}.emevd.dcx', path))
    return found


def build(base, mods, out, log=print):
    """Apply the event edits of `mods` [(name, folder)], in order, to the files `base(game path)`
    gives (bytes, or None if there is none). Writes the results under `out`/dvdroot_ps4 and
    returns `out`, or None when no edit applied."""
    by_file = {}
    for name, folder in mods:
        for relative, path in edit_files(folder):
            by_file.setdefault(relative.as_posix().casefold(), (relative, []))[1].append((name, path))
    written = 0
    for relative, edits in by_file.values():
        original = base(relative)
        if original is None:
            log(f'Script edits: {relative.as_posix()} is not in the game; '
                f'left out: {", ".join(name for name, _ in edits)}')
            continue
        try:
            data = dcx_decompress(original)
            Emevd(data)
        except (EmevdError, FormatError) as error:
            log(f'Script edits: {relative.as_posix()} can\'t be read ({error}); '
                f'left out: {", ".join(name for name, _ in edits)}')
            continue
        applied = []
        for name, path in edits:
            try:
                data = apply(data, parse(path.read_text(encoding='utf-8')))
            except (EditError, OSError, UnicodeDecodeError) as error:
                log(f'Script edits: {name}: {path.name}: {error}; left out')
                continue
            applied.append(name)
        if not applied:
            continue
        target = Path(out) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(dcx_compress(data, template=original))
        written += 1
        log(f'Script edits: {relative.as_posix()}: {", ".join(applied)}')
    return Path(out) if written else None
