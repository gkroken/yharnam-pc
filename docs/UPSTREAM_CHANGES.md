# Changes to upstream yharnam-pc files

The `plugins` branch keeps its own code in new files. This list covers every change it makes
to a file that came from upstream (Jgamrot/yharnam-pc), so each rebase knows what to carry over
and what to look at. New files (plugin host, header, tests) aren't listed here.

| File | Change | Why |
|---|---|---|
| `src/engine_features.c` | `bb_engine_install` calls `bb_plugins_install()` after the port's own features, before the main-frame tick goes in. | Plugins load while hooks are still open (before `bb_hooks_seal`). |
| `src/engine_hooks.c`, `.h` | Adds `bb_image_size()`, `bb_hooks_sealed()`, `bb_image_code()`, `bb_image_is_code()` and `bb_hook_code()`. | The plugin host's address checks; `write()` keeps out of code; stub space for plugin hooks (`plugin_hooks.c`). |
| `src/probe.c` | Before `bb_engine_install`, reports each executable segment with `bb_image_code()`. | So `write()` knows where code is, whatever the pages' protection (all RW until after the seal, and RWX in co-op). |
| `gpu/CMakeLists.txt` | `bb-probe` builds `src/plugin_host.c` and `src/plugin_hooks.c` with `include/`. Adds `plugin-host-test` with its ten test plugins and three hook test plugins. | |
| `run.py` | Sets `BB_EBOOT_109` (the community patches' 1.09 test) and `BB_EBOOT_SHA256` (of `eboot.elf`). | `eboot_is_109()` and `eboot_sha256()`. |
| `.gitignore` | `/plugins/*.dll`. | Plugins dropped into a source tree. |
| `tests/test_probe.py` | `EXE` is `out/bb-probe.exe` on Windows. | The loader tests skipped on Windows because the file has an `.exe` suffix there. 13 of 14 now run and pass; `test_illegal_instruction_reports_guest_offset` expects a POSIX signal code. |
