# Native plugins

A plugin is a DLL that runs inside the game. It is started once, while the game's code is
still open to hooks, and can then run code every frame on the game's main thread. The API is
bbhost's (`include/bbhost_plugin.h`, by droogie, GPL-3.0-or-later), so a plugin written for
bbhost builds here unchanged. This file says what this host does and how it differs from bbhost.

## Where plugins go

| Folder | What it is for |
|---|---|
| `<data>\plugins` | Next to the game's folders: `bbport-windows\plugins` in a package, `port\plugins` in a source tree. |
| `<user>\plugins` | Beside the saves (`BB_GPU_USER_DIR`, default `<data>\user`). A file here replaces a file of the same name above. |

Only `*.dll` files directly in a folder load, in name order. A plugin that needs other DLLs keeps
them in a subfolder of its own, and Windows looks in the plugin's own folder first when it loads
them. `BB_PLUGIN_DIRS` (separated by `;`) replaces both folders; the tests use it.

## Turning plugins on and off

- **All of them:** `BB_PLUGINS=0` loads none. `BB_ENGINE_HOOKS=0` turns off the engine hooks, and
  plugins with them.
- **One at a time:** `<user>\plugins.ini`:
  ```ini
  [plugins]
  randomizer = 0      ; off
  boss_rush = 1       ; an opt-in plugin, turned on

  [randomizer]
  seed = hunter       ; the plugin reads it with config("randomizer.seed")
  ```
  `1`, `true` and `on` turn a plugin on; `0`, `false` and `off` turn it off. A plugin flagged
  `BB_PLUGIN_OPT_IN` stays off until it is turned on.
- **Online play** (`BB_ONLINE=1` without seamless co-op's `BB_COOP=1`) leaves out every plugin
  flagged `BB_PLUGIN_GAMEPLAY`. This is the same rule that turns cheats off online.

The log (`user\last_run.log`) says which plugins loaded and why any were left out (`Plugins:`
lines). A plugin's own `log()` lines start `Plugin <name>:`.

## The start

1. Every DLL is loaded and `bb_plugin_info` is read. A DLL is left out, with a log line, when:
   - it exports neither `bb_plugin_init` nor `bb_plugin_image`;
   - its name isn't 1–63 letters, digits, `_` or `-`;
   - another plugin already has its name;
   - it was built for a newer API than this host's (16);
   - plugins.ini or online play turns it off.
2. `bb_plugin_init(api)` runs for each plugin, then `bb_plugin_image(api)` for each. Returning
   anything but 0 unloads the plugin, together with every callback it registered.
3. Both run on the process's main thread, from `bb-probe`'s start (`src/probe.c`), before any
   of the game's code runs. Here the image already exists
   during `bb_plugin_init`, but the image functions still refuse until `bb_plugin_image`, as on
   bbhost.

## What this host provides (API 16)

| Function | Here |
|---|---|
| `log`, `config`, `data_dir`, `mods_dir`, `plugin_dir` | Yes. `config` reads `plugins.ini`, and answers `host.name` with `yharnam-pc`, so a plugin can tell where it runs. |
| `eboot_is_109`, `eboot_sha256` | Yes. "1.09" is the test the community patches use: the code at every patch site matches. The SHA-256 is of the port's `eboot.elf`. That isn't the same file bbhost hashes, so it differs from bbhost's 1.09 digest; compare with `eboot_is_109()`, not the digest. |
| `guest_addr`, `read`, `write`, `patch` | Yes. See the notes below the table. |
| `on_frame` | Yes. On the game's main thread, once a frame, right after its frame-time manager's update. |
| `hook`, `hook2` | Yes, from `bb_plugin_image` only (see below). |
| `call_guest` | Yes, once the game runs (not during `bb_plugin_image`, before its own start), from its main thread only (`on_frame`, or a hook running there), and only to an address in the image's code. It sets `al` for a variadic callee. |
| `event_flag_get`, `event_flag_set`, `on_event_flag` | Yes (`src/plugin_game.c`). See below. |
| `player_stat_get`, `player_stat_set` | Yes, by bbhost's names: `hp`, `max_hp`, `stamina`, `max_stamina`, `vitality`, `endurance`, `strength`, `skill`, `bloodtinge`, `arcane`, `insight`, `level`, `echoes`, plus the save's counters `ng_cycle`, `coop_helps`, `invader_kills`, `true_deaths`, `deaths`, `play_time_ms`. At the title the record already exists and reads 0s, as on bbhost. `set` works from the main thread only. |
| `param_row`, `param_get`, `param_set`, `param_ids` | Yes (`src/plugin_params.c`). See below. |
| `chr_list`, `sp_effect_has`, `sp_effect_apply` | Yes. See below. |
| `show_message`, `show_text` | Yes, from any thread. A banner low in the middle (`show_message`), and one line placed by the plugin (`show_text`: x and y as fractions of the window, `BB_TEXT_*` alignment, scale). Each replaces the last of its kind, and fades over its last half second. The overlay draws them over the game, the menus included. |
| `on_world_load` | Yes. On the main thread, at the start of a frame, when the world gets a new main player (every load builds one) or the player is in another block (a warp). It gets the block, packed as `player_block` packs it. This is bbhost's test; a load's screen may still be up. |
| `player_position`, `player_block` | Yes. They return 1 while the world has no player (the title, a load). |
| `input` | Yes, from the main thread. What the keyboard (by SDL scancode) and mouse buttons hold (a key tapped since the last read reads as held once, so a tap shorter than a frame is still seen), and the clicks, motion and wheel since the last read. These are counted apart from the game's own reads, so reading takes nothing from the camera or the pad. While another window has the focus nothing is held or pressed. `playing` is 1 in the world (a main player exists, so not at the title or during a load) with no menu, text box or overlay up and the window focused, mouse camera on or off. Motion counts only with no menu, text box or overlay up. One reader, as on bbhost: two plugins reading share the presses. Returns 1 without a window. `input_take` is not yet in. |
| `register_hle` | No: this port's system functions are its own runtime. Returns 1 and logs it. |
| `visitor` | Always 0: this host never loads a plugin as a visitor. |
| Everything else | Not yet. Each returns the failure value its comment in the header gives (1, 0, NULL or -1) and logs once that it is missing. |

**Memory functions:**
- **Addresses:** `read` and `write` take a Binary Ninja address inside the image, or a runtime
  address. The game runs natively here, so its pointers are the process's own. Any address
  outside the image is used as it is, unlike bbhost, which accepts only memory the game mapped.
  `read` of an unmapped address returns 1. `write` returns 0 for any writable memory, the host's
  included, so check addresses before writing.
- **`write` never changes code:** it refuses any address in the image's executable segments, in
  every phase. This holds even during `bb_plugin_image`, when the whole image is still writable,
  and in co-op, where code stays writable. Code goes through `patch`, which checks the expected
  bytes first. `patch` returns 0 when the new bytes are already there, as on bbhost.

**Hooks** (`src/plugin_hooks.c`):
- **When:** only during `bb_plugin_image`, while the engine hooks are open. Earlier or later, a
  hook returns 2 and logs why.
- **The prologue:** the function's first `n` bytes, 5 to 64 of them, whole instructions, none of
  them RIP-relative or a branch. bbhost asks for at least 14; this host accepts 5.
  - The host checks that the bytes are there, and returns 1 if they aren't.
  - It can't tell where instructions end, or whether one is RIP-relative. That stays the
    plugin's job, as on bbhost.
  - With `prologue` NULL (`hook2`), the bytes there are taken as they are, but never when they
    start with a jump or call: rel32 (`E9`/`E8`), or through rip (`FF 25`/`FF 15`). Copying
    one into the stub would send execution elsewhere.
- **The image in memory isn't `eboot.elf`:** the game reads its thread block with
  `mov rax, fs:[0]` in the ELF (`64 48 8b 04 25 …`). The port rewrites it to
  `mov rax, gs:[0]` (`65 …`), which is about 17,000 loads, 3,500 of them near a function's
  entry. A prologue copied from a disassembly of `eboot.elf` then doesn't match, and the hook
  returns 1.
  - Take the bytes from memory instead: `read()` them in `bb_plugin_image`.
  - Or pass a NULL prologue.
  - The host gives the stub's copy of such a load the same thread-block displacement the
    image's own loads get. A prologue that cuts a load off is refused (2).
- **Bytes already rewritten:** a hook is refused (2) whatever its prologue argument, if any of
  its bytes were rewritten by:
  - any of the port's own hooks (entry, call-site, mid-function, skip or store) or patches;
  - another plugin's hook at a different address;
  - a `patch()` call.

  Hooking over them would cut that jump in half or copy a moved one. A second hook at exactly
  the same address joins the chain instead (below).

  Some rewrites aren't recorded, so for those only the prologue check protects a hook (when a
  prologue is given):
  - the community patches applied as the image loads (`patches/Bloodborne.xml`);
  - the thread-block loads (above), which are rewritten after plugins hook, so a hook's stub
    gets its own rewrite;
  - Hunter's Atlas lamp travel (opt-in, `BB_MAP_TRAVEL_ENABLED=1`). It writes a `jmp [rip]`
    over the first 17 bytes of SprjFlipper::Update (BN `0x2434770`) before plugins load. With
    it on, a plugin's hook there is refused: the bytes differ, or the `FF 25` guard catches a
    NULL prologue.
- **What the callback can do:** each `hook2` callback gets a `BbHookCtx`, and may rewrite
  arguments and the low halves of `xmm0`-`xmm7`.
  - Returning nonzero skips the function. Its caller gets `ctx->ret` in `rax`, and in `xmm0`'s
    low 64 bits too. bbhost sets `rax` only, so a skipped function that returns a float or
    double returns garbage there.
  - A `hook` (v1) callback sees the integer arguments read-only. If it skips, the caller gets 0.
- **Several plugins on one function:** each hook joins that function's chain, in load order.
  - Each callback sees the arguments the one before it left. The first that skips decides, and
    the rest don't run.
  - A later hook must give the same length as the first, and the same original bytes.
- **If a plugin fails its start** after hooking, its callbacks are dropped. The stub stays in the
  code and runs the rest of the chain.
- **Callbacks run on whatever thread calls the function, on that thread's stack.** Game threads'
  stacks can be small, so keep callbacks short. Do heavy work in `on_frame`.
- **The stub:** it saves `rdi`-`r9`, `rax` and `xmm0`-`xmm7`, calls the chain, reloads them,
  then runs the displaced prologue and jumps back. At a function's entry the caller's red zone
  is already given up, so the stub may use the stack.

**Event flags** (the world's state as numbered bits: bosses, doors, NPC steps):
- **`event_flag_get`:** reads the game's flag store directly, with fault-safe reads, so it works
  from any thread. It returns 1 while the flag's block doesn't exist (yet). In game it matches
  the game's own reader (`call_guest` on BN `0x17cfc00`) for every flag compared.
- **`event_flag_set`:** writes the bit, from the game's main thread only.
- **`on_event_flag`:** calls back with every change, oldest first, on the main thread, at the
  start of the next frame (before `on_frame`). That covers the game's single-flag writes and
  plugins' own.
  - It works through a hook on the game's flag setter (BN `0x17cfcc0`), placed when the first
    plugin registers. So register in `bb_plugin_image`. After the start, registering works only
    if another plugin already started the watcher.
  - Writes the game makes through its range setters and save loads aren't reported.
  - Each frame delivers the changes that waited when it began. A callback's own
    `event_flag_set` (or anything a callback triggers) arrives next frame, so callbacks that keep
    forcing a flag can't hang the game.
  - The queue holds 1024 changes between frames; more are counted, and the log says how many
    were dropped.
  - The watcher's hook on the setter is 8 bytes long. A plugin that hooks that setter itself
    with another length (bbhost asks for at least 14) can't share it: whichever comes second
    gets 2. Hook the setter yourself, or use `on_event_flag`, not both.

**Params** (the game's data tables: weapons, armour, enemies, effects, bullets, ...):
- **Tables:** named as the game names them: `EquipParamWeapon`, `EquipParamProtector`,
  `NpcParam`, `SpEffectParam`, `Bullet`, `CharaInitParam`, `Magic`, and so on.
- **Rows:** `param_row` gives a row's address and size, and `param_ids` lists the row ids in
  order.
- **Fields:** `param_get` and `param_set` use the game's own field names (`weight`,
  `correctStrength`, `attackBasePhysics`; `name[2]` for an array element).
  - The names come from the game's PARAMDEF files. `scripts/paramdefs.py` writes them out at
    each start (`out\paramdefs.txt`), and the log says "Param definitions: 63 of 63".
  - A definition is used only when its row size equals the live table's row stride. Otherwise
    its fields are refused and the log says why, so a wrong definition can't write into the
    wrong bytes.
- **Values are text:**
  - integers, checked against the field's range;
  - `f32` as a number;
  - bitfields as their value; writing one keeps the other bits of its byte;
  - fixed strings read as text and can't be written.
- **Rules:** `param_set` works from the main thread only. A change applies to every later use
  of the row; characters already built from it may keep their copy (as bbhost notes).

**Characters and SpEffects:**
- **`chr_list`:** the characters the world holds, the main player first (`is_player` 1), then
  everyone in the game's handle-indexed character sets, each once.
  - Each `BbChr` gives its handle, NpcParam id (-1 for a player), type, team, HP, position and
    model number (2560 for c2560).
  - The `ins` pointers are good until the world changes, so use them within the frame.
  - Main thread only (`on_frame`, or a hook running there), as bbhost's header says. Off it,
    it returns 1. `sp_effect_has` works from any thread.
- **`sp_effect_has`:** whether an effect is on a character now.
- **`sp_effect_apply`:** puts an effect on a character (`NULL` is the main player), the way the
  game's scripts do.
  - Main thread, once the game runs.
  - Refused (1) unless the id is a row of SpEffectParam, and unless the character is one
    `chr_list` returns this moment. A stale pointer never reaches the game's code.

One limit, for tables of a single row: there's no row stride to compare, so the size check
above can't run, and the game's definition is used as it is.

**Careful with two missing functions:** their failure values read as "all clear".
- `loading_screen()` returns 0, which means "no loading screen".
- `key_bound()` returns 0, which means "no game action on this key".

A bbhost plugin that waits for `loading_screen() == 0`, or picks keys by `key_bound()`, won't be
protected by them here until they're provided.

`docs/WORKPLAN.md` in the parent repository gives the order the rest arrives in: the loading
screen and warps next.

## Building a plugin

Use clang or MinGW. A callback the game calls directly needs the System V calling convention
(`BB_GUEST_ABI`), which MSVC can't produce. Plain callbacks (`on_frame` and the rest) are
ordinary C. For example:

```sh
clang -shared -O2 -Iport/include -o hello.dll hello.c
```
