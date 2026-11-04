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
| `guest_addr`, `read`, `write`, `patch` | Yes. `read` and `write` take a Binary Ninja address inside the image, or a runtime address: the game runs natively here, so its pointers are the process's own. `write` refuses memory that isn't writable. `patch` checks the expected bytes, then writes. |
| `on_frame` | Yes. On the game's main thread, once a frame, right after its frame-time manager's update. |
| `register_hle` | No: this port's system functions are its own runtime. Returns 1 and logs it. |
| `visitor` | Always 0: this host never loads a plugin as a visitor. |
| Everything else | Not yet. Each returns the failure value its comment in the header gives (1, 0, NULL or -1) and logs once that it is missing. |

`docs/WORKPLAN.md` in the parent repository gives the order the rest arrives in: hooks and
`call_guest` first, then event flags, params, the player and characters.

## Building a plugin

Use clang or MinGW. A callback the game calls directly needs the System V calling convention
(`BB_GUEST_ABI`), which MSVC can't produce. Plain callbacks (`on_frame` and the rest) are
ordinary C. For example:

```sh
clang -shared -O2 -Iport/include -o hello.dll hello.c
```
