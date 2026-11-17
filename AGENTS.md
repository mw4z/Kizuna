# Notes for coding agents (Codex, Claude Code, ...)

## Python environment

There is no virtual environment. The live agent (watcher.py, started by the "Anime FYA" scheduled
task) runs on the user's main install, so run and test with exactly that interpreter:

    C:\Users\moody\AppData\Local\Programs\Python\Python313\python.exe

It already has every package in `requirements.txt` (numpy, psutil, watchdog, libtorrent, pillow, opencv,
winrt OCR).
A bare `python` from another environment (or a sandbox) usually lacks numpy/psutil and every test
module fails at import. Do not pip-install into, upgrade or replace that interpreter without asking
the user. If you need an isolated environment, create one that reuses its packages:

    & "C:\Users\moody\AppData\Local\Programs\Python\Python313\python.exe" -m venv --system-site-packages .venv-test

## Running the tests (PowerShell, from this folder)

    $py = "C:\Users\moody\AppData\Local\Programs\Python\Python313\python.exe"
    & $py -W ignore::ResourceWarning -m unittest test_agent test_audit test_audit_fixes test_dkb test_fansub test_fillers test_mandatory_processing test_robust test_stremio test_stremio_batch test_reliability test_shows test_settings
    node test_picker.cjs

Expected: 256 tests OK (2026-11-18, about 2 minutes: test_reliability runs real ffmpeg/mkvmerge, Windows
file locks and a slow proxy stream) and "Picker checks passed".

Rules the 2026-11-18 reliability audit added (keep them):
- Write shared JSON (progress, state, queue, job, batch) only through `agent.write_atomic`: Windows refuses
  the swap while the status page has the file open, so it retries; readers in the same process take the lock.
- The mux may only swap in a `.tmp.mkv` this run built and verified (`mux.Locked`); stale ones are deleted.
- Tests must patch `library_processing.FILE`/`ROOT` (or use `Sandbox`): an unpatched submit writes the queue
  next to the code. Tests that touch shows must patch `shows.FILE`/`shows.STATE` (the existing tests use
  `fixtures/shows.json`), and settings tests `settings.FILE`; never read the user's shows.json/settings.json.
- The followed shows live in the private shows.json (Weekly page), not in code. DKB (dkb.py) and the Stremio fetcher
  (shows.stremio_poll) race for each new episode: claim() + have_episode() keep it to one source. Each poller owns its
  state file; other threads ask for changes with shows.request_reset().
- UI: every page links kizuna.css + kizuna.js and keeps its element ids (test_picker.cjs drives stremio.html's script).

Sandboxed agents (e.g. Codex): every Python on this PC lives under `C:\Users\moody\AppData\...`, which the
sandbox cannot see ("CommandNotFoundException"). Do not give up or install anything: request escalated
permissions / run the test command outside the sandbox and let the user approve it (the user chose this,
2026-11-17). The tests only use temp folders.
Some tests use real tools on tiny temp files: ffmpeg/ffprobe on PATH (WinGet), MKVToolNix in
`C:\Program Files\MKVToolNix`, Node.js for test_picker.cjs.

## Safety rules (live system)

- This folder IS the live agent. Develop in a copy, run the suite there, check the live files changed
  only by your diff, back up to `backup\<date>-<label>\`, then copy in.
- Never run agent.py / processing_worker.py / cleanup.py against the real folders while testing;
  tests sandbox every path (see `Sandbox` in test_audit.py). `python cleanup.py --dry` is safe.
- Never open `C:\ProgramData\Jellyfin\Server\data\jellyfin.db` directly (not even read-only): use
  `agent.jellyfin_db()` (reads a temp copy). A direct reader once recreated jellyfin.db-shm/-wal as the
  user and locked the Jellyfin service out of its own database.
- Do not edit state.json, library-processing.json, subtitle_policy.json, stremio-account.bin or
  anything in the user's libraries/inbox without the user asking.
- Restart only the watcher child (`watcher.py --child`; the supervisor relaunches it) and only when no
  agent.py / processing_worker.py process is running.
