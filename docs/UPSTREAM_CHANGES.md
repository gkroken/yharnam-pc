# Changes to upstream yharnam-pc files

The `plugins` branch keeps its own code in new files. This list covers every change it makes
to a file that came from upstream (Jgamrot/yharnam-pc), so each rebase knows what to carry over
and what to look at. New files (plugin host, header, tests) aren't listed here.

| File | Change | Why |
|---|---|---|
| `tests/test_probe.py` | `EXE` is `out/bb-probe.exe` on Windows. | The loader tests skipped on Windows because the file has an `.exe` suffix there. 13 of 14 now run and pass; `test_illegal_instruction_reports_guest_offset` expects a POSIX signal code. |
