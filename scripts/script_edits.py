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

A mod adds an option to an NPC's talk menu with `edits/talk/<map>.talk.edit.toml`, which edits
`script/talk/<map>.talkesdbnd.dcx`:

    [[option]]
    script = "t241335"            # the talk script in that file (t<...>.esd)
    text = 15000000               # the message the option shows: an id of the game's talk text
    flag = 12401902               # turned on when the option is picked; the talk then ends
    menu = 1                      # optional: which menu, when the script has two (1 or 2)

The option goes last in the menu, and nothing else in the script changes: an event (an event
edit) can wait for its flag. Only menus of the game's usual shape take one (the choice read and kept, one branch
for a closed menu or Leave); any other is refused.

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
import esd  # noqa: E402
from emevd import Emevd, EmevdError  # noqa: E402
from fromsoft import Bnd4, FormatError, dcx_compress, dcx_decompress  # noqa: E402

EDITS_FOLDER = 'edits'
EVENT_SUFFIX = '.emevd.edit.toml'
TALK_SUFFIX = '.talk.edit.toml'


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

# Built-in instructions: name -> (bank, id, ((type, enum or None), ...)). Five layouts are the ones
# the port's own event edits write in the game (scripts/boss_rematches.py: IfEventFlag,
# EndUnconditionally, EndIfEventFlag, SetEventFlag, WarpPlayerToRespawnPoint). IfConditionGroup's
# and AwardItemLot's match every use of 0:0 and 2003:4 in the game's own event files
# (port-a2866f9-N1).
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
        try:
            data.extend(struct.pack('<' + code_char, value))
        except (struct.error, OverflowError) as error:  # 1e40 as an f32 (port-a2866f9-F1)
            raise EditError(f'{value!r} does not fit a {kind} ({error})') from None
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


@dataclass
class TalkOption:
    script: str
    text: int
    flag: int
    menu: int = 1


def parse_talk(text):
    """The options of one talk edit file's TOML text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise EditError(f'not valid TOML ({error})') from None
    if set(data) - {'option'}:
        raise EditError(f'unknown keys {sorted(set(data) - {"option"})}: a talk edit file has [[option]] tables')
    options = data.get('option', [])
    if not isinstance(options, list) or not options:
        raise EditError('no [[option]] tables')
    result = []
    for number, option in enumerate(options, 1):
        where = f'option {number}'
        unknown = set(option) - {'script', 'text', 'flag', 'menu'}
        if unknown:
            raise EditError(f'{where}: unknown keys {sorted(unknown)}')
        script = option.get('script')
        if not isinstance(script, str) or not script.startswith('t') or not script[1:].isdigit():
            raise EditError(f'{where}: script is a talk script\'s name, such as "t241335"')
        values = {}
        for key, low in (('text', 0), ('flag', 1)):
            value = option.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= 0x7fffffff:
                raise EditError(f'{where}: {key} must be a number from {low}')
            values[key] = value
        menu = option.get('menu', 1)
        if isinstance(menu, bool) or menu not in (1, 2):
            raise EditError(f'{where}: menu is 1 or 2')
        result.append(TalkOption(script, values['text'], values['flag'], menu))
    return result


SHOW_MENU = (1, 10)      # ShowShopMessage: shows the talk list built before it
ADD_TO_LIST = (1, 19)    # AddTalkListData(index, text, -1)
SET_FLAG = (1, 11)       # SetEventFlag(flag, state)
# The menu's first condition reads the choice (GetTalkListEntryResult, call 23) and keeps it in
# register 0 (0xa7); the others read it back (0xaf). The branch that ends the talk compares it
# with 0 (the menu closed), in the first condition or a later one, or asks whether the menu is
# closed (call 53 == 0). Seen in 51 of the game's 53 menus.
READ_CHOICE = esd.lit(23) + b'\x84\xa7'
CLOSED = (b'\xaf' + esd.lit(0) + b'\x95', READ_CHOICE + esd.lit(0) + b'\x95',
          esd.call(53, []) + esd.lit(0) + b'\x95')
EQ = b'\x95'


def add_option(data, option):
    """The talk script `data` (.esd bytes) with `option` added to its menu."""
    script = esd.parse(data)
    menus = [(g, si) for g in script.groups for si in g.states
             if script.states[si].entry >= 0 and SHOW_MENU in
             [(script.cmds[c].bank, script.cmds[c].id) for c in script.runs[script.states[si].entry].items]]
    if not menus:
        raise EditError(f'{option.script} has no talk menu')
    if option.menu > len(menus):
        raise EditError(f'{option.script} has {len(menus)} menu(s), not {option.menu}')
    group, site = menus[option.menu - 1]
    state = script.states[site]
    if group.states and group.states[0] == site:
        raise EditError(f'{option.script}: its menu is where its state group starts, which this can\'t edit')
    conds = list(script.runs[state.conds].items) if state.conds >= 0 else []
    codes = [script.exprs[script.conds[c].eval].code for c in conds]
    if not codes or not codes[0].startswith(READ_CHOICE):
        raise EditError(f'{option.script}: its menu isn\'t of the usual shape (the choice isn\'t read first)')
    closing = [k for k, code in enumerate(codes) if any(mark in code for mark in CLOSED)]
    if len(closing) != 1:
        raise EditError(f'{option.script}: its menu isn\'t of the usual shape '
                        f'({len(closing)} branches for a closed menu)')
    leave = script.conds[conds[closing[0]]].target
    # The new entry's index: one past every index the script's talk lists use.
    used = []
    for cmd in script.cmds:
        if (cmd.bank, cmd.id) == ADD_TO_LIST and cmd.args >= 0 and script.runs[cmd.args].items:
            code = script.exprs[script.runs[cmd.args].items[0]].code
            if code[-1:] == b'\xa1' and (len(code) == 2 and 0x40 <= code[0] < 0x80):
                used.append(code[0] - 0x40)
            elif code[:1] == b'\x82' and len(code) == 6:
                used.append(struct.unpack_from('<i', code, 1)[0])
    index = max(used, default=0) + 1
    if not 0 < index < 0x7fffffff:
        raise EditError(f'{option.script}: no index left for a new option')

    def lit(value):
        return esd.done(esd.lit(value))

    # Add the entry just before the menu shows; a new branch after the first (which reads the
    # choice) goes to a new state that turns the flag on and then leaves as a closed menu does.
    entry = list(script.runs[state.entry].items)
    show = next(k for k, c in enumerate(entry) if (script.cmds[c].bank, script.cmds[c].id) == SHOW_MENU)
    entry.insert(show, script.cmd(*ADD_TO_LIST, [lit(index), lit(option.text), lit(-1)]))
    picked = script.state(max(script.states[si].id for si in group.states) + 1)
    group.states.append(picked)
    script.states[picked].entry = script.run([script.cmd(*SET_FLAG, [lit(option.flag), lit(1)])])
    script.states[picked].conds = script.run([script.cond(leave, esd.done(esd.lit(1)))])
    branch = script.cond(picked, esd.done(b'\xaf' + esd.lit(index) + EQ))
    state.entry = script.run(entry)
    state.conds = script.run(conds[:1] + [branch] + conds[1:])
    result = esd.serialize(script)
    if esd.serialize(esd.parse(result)) != result:
        raise EditError(f'{option.script}: the edited script doesn\'t read back the same')
    return result


def apply_talk(data, options):
    """The talk archive `data` (BND4 bytes) with `options` added (bytes in, bytes out)."""
    try:
        archive = Bnd4(data)
        for option in options:
            try:
                i = archive.index('\\' + option.script + '.esd')
            except FormatError:
                raise EditError(f'{option.script}: not a talk script in this file') from None
            archive.files[i] = add_option(archive.files[i], option)
        return archive.to_bytes()
    except (esd.EsdError, FormatError) as error:
        raise EditError(str(error)) from None


# The kinds of edit file: its folder in edits/, its suffix, the game file's folder and suffix,
# a check that the game file can be edited, the reader and the applier.
KINDS = {
    'event': (EVENT_SUFFIX, 'event', '.emevd.dcx', Emevd, parse, apply),
    'talk': (TALK_SUFFIX, 'script/talk', '.talkesdbnd.dcx', Bnd4, parse_talk, apply_talk),
}


def _child(folder, name):
    """`name` in `folder`, matched case-insensitively, as mods.child does."""
    for entry in (folder.iterdir() if folder.is_dir() else ()):
        if entry.name.casefold() == name.casefold():
            return entry
    return folder / name


def edit_files(mod):
    """(kind, game path, edit file) of a mod's edits: <mod>/edits/event/<name>.emevd.edit.toml
    and <mod>/edits/talk/<name>.talk.edit.toml."""
    found = []
    for kind, (suffix, game_folder, game_suffix, *_rest) in KINDS.items():
        folder = _child(_child(Path(mod), EDITS_FOLDER), kind)
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
            if path.is_file() and path.name.casefold().endswith(suffix):
                stem = path.name[:-len(suffix)]
                found.append((kind, Path('dvdroot_ps4') / game_folder / f'{stem}{game_suffix}', path))
    return found


def build(base, mods, out, log=print):
    """Apply the edits of `mods` [(name, folder)], in order, to the files `base(game path)`
    gives (bytes, or None if there is none). Writes the results under `out`/dvdroot_ps4 and
    returns `out`, or None when no edit applied."""
    by_file = {}
    for name, folder in mods:
        for kind, relative, path in edit_files(folder):
            by_file.setdefault(relative.as_posix().casefold(), (kind, relative, []))[2].append((name, path))
    written = 0
    for kind, relative, edits in by_file.values():
        _suffix, _folder, _game_suffix, check, read, edit = KINDS[kind]
        original = base(relative)
        if original is None:
            log(f'Script edits: {relative.as_posix()} is not in the game; '
                f'left out: {", ".join(name for name, _ in edits)}')
            continue
        try:
            data = dcx_decompress(original)
            check(data)
        except (EmevdError, FormatError, esd.EsdError) as error:
            log(f'Script edits: {relative.as_posix()} can\'t be read ({error}); '
                f'left out: {", ".join(name for name, _ in edits)}')
            continue
        applied = []
        for name, path in edits:
            try:
                # utf-8-sig: Windows editors and PowerShell 5.1 write a BOM (port-a2866f9-F2).
                data = edit(data, read(path.read_text(encoding='utf-8-sig')))
            except (EditError, OSError, UnicodeDecodeError) as error:
                log(f'Script edits: {name}: {path.name}: {error}; left out')
                continue
            except Exception as error:  # noqa: BLE001 - a mod's data must never stop the start
                log(f'Script edits: {name}: {path.name}: could not be applied '
                    f'({type(error).__name__}: {error}); left out. Please report this.')
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
