# Kizuna operations guide

Kizuna gives every new weekly anime episode styled Arabic subtitles, unattended, and puts it in
Jellyfin so it plays on the TVs.

## Shows (the Weekly page)

Followed shows are managed on the **Weekly** page (`/weekly`) and stored in the private, git-ignored `shows.json`;
nothing is hard-coded. Changes apply on the next check without a restart.

- **Add:** the page recommends what is airing this season (AniList: new shows first, then continuing ones) and searches
  any anime. Choosing one fills the form automatically: the Jellyfin folder (English title and the year of season 1,
  following AniList prequels of the same series), the season, the DKB release name (looked up on DKB) and the Stremio
  catalog entry (season, latest aired episode, and an episode offset when DKB and Stremio number episodes
  differently). Start with the next new episode (no backfill) or a chosen episode.
- **Sources:** DKB 1080p weekly releases and/or your Stremio add-ons. Both check every few minutes (Settings); whichever
  has a new episode first downloads it. `shows.claim()` and `shows.have_episode()` make the other source skip it
  (library folder, Anime library, inbox, Arabic preparation queue and both sources' records are checked).
  Stremio downloads use your Settings languages and resolutions and go through the normal Arabic preparation into
  Weekly Anime. An episode no Stremio source offers within 3 days is shown as "not found on Stremio" (DKB can still
  bring it). A Stremio download you cancel is not fetched again by Stremio; DKB may still bring it.
- **Arabic subtitles rule per show:** official or fan, fan first, fan only, or typeset by hand (`manual`: never
  published automatically). Shows not on the list still use `subtitle_policy.json`.
- **Edit:** changing the season, start episode or release name restarts both sources from the new point. Renaming the
  folder moves its episodes; it is refused while an episode of the show is still downloading or being prepared.
- **Remove:** the show's folder moves to the Anime library (nothing is deleted; if a same-named folder exists there
  it stays in Weekly Anime). Refused while an episode is in flight.

The Now page shows each show's next episode, air time and a countdown from AniList's public schedule (refreshed every
30 min), using the AniList id saved with the show. Times are the Japanese broadcast; the Arabic-subbed episode arrives
some hours later.

## Settings

The **Settings** page (`/settings`, private `settings.json`, every value validated) has separate **Anime** and
**Movies & series** sections: audio languages (up to three, in order: anime defaults to Japanese, movies & series to
English), an optional extra subtitle track (kept when a source has it, never required), resolutions (highest first),
the Discover audio filter and default libraries. Weekly Stremio fetches try every resolution of the first language
before the next language. Shared: how often DKB and Stremio check (5–240 min), 12/24-hour air times, how long watched
Weekly Anime/Anime episodes stay before the Recycle Bin (1 h – 30 days; a change applies to already-watched episodes
from the next hourly cleanup) and Windows "ready" notifications. A damaged settings.json keeps the last good values,
and cleanup then waits the longest allowed time rather than guess.

## Interface language

Settings → Interface language switches the whole app between English and Arabic. In Arabic the server serves every
page right-to-left from the first paint, and `kizuna.js` translates the interface and the agent's messages with
`kizuna-ar.js` (exact strings, then patterns for messages with numbers, dates or names; titles, release names, file
names and the log are never translated). Dates use Arabic with Western digits. Library shows use Jellyfin's own
names, posters and overviews (your Jellyfin metadata language); titles that aren't in the library keep the source's
artwork and names, because AniList and the Stremio catalogs have no Arabic.

## News

The **News** page shows anime (Crunchyroll News, Anime News Network, Anime Corner, MyAnimeList) and movies & TV
(Deadline, Variety, Collider) headlines from their public RSS feeds. Feeds are re-read in the background every 3
minutes and the open page checks every minute ("N new stories"). Feed content is untrusted: text is stripped of
HTML, links must be http(s), images https, each feed is size-limited and at most 25 stories per source are kept.
Articles open on the publisher's site. Arabic sources: IGN Arabia (its film, TV and anime stories only) and
Youm7's art section; with the Arabic interface they come first and English stories carry an "English" label.

## What happens when an episode lands

**Automatic DKB downloads:** the watcher checks DKB's weekly listings every 20 minutes
for every show in `SHOWS` in `dkb.py`. Only DKB 1080p weekly episodes
are selected. `dkb-state.json` records each show's initial latest episode, so older
episodes are not downloaded. Completed episodes are remembered across restarts and
episodes already in the watched folder or Jellyfin library are skipped.

Downloads use the existing libtorrent helper in `work\dkb\`, with a 40-minute limit
per attempt. Incomplete downloads stay there for a later retry; they never enter the
watched folder. After torrent completion and size/video checks, the episode is copied
as `.part` and renamed to `.mkv`, triggering the pipeline below. Existing files are
never overwritten. DKB errors are recorded in `log.txt` and retried next check.

The first successful check establishes each show's baseline. To initialize without
downloading, run `python dkb.py --initialize`. Keep `dkb-state.json` to preserve the
starting point. Show subscriptions are the `SHOWS` tuple in `dkb.py`; the Jellyfin
folder map in `agent.py` is separate. The DKB listing is limited to the site's recent
results, so very long offline periods can miss episodes that leave that listing.

1. **`watcher.py`** sees a new `.mkv` in
   `C:\Users\moody\OneDrive\Videos\أنمي\آخر حلقات الأنمي` and waits until its size has been
   stable for 60 s (download or OneDrive sync finished).
2. It starts **`agent.py --now`**. Files with a non-commentary Arabic audio track skip
   all subtitle extraction/search, styling, timing checks, and muxing; their original
   video is copied to Jellyfin if needed. This includes Arabic-dubbed episodes of `manual` shows. Detection
   uses audio language tags (`ara`, `ar`, regional `ar-*`) or an explicit Arabic audio
   track name; it does not infer audio language from subtitle tags or listen to untagged audio.
   Other files without an `العربية` track:
   - takes the official Arabic SRT inside the file, or, if there is none, the Arabic track of a
     Judas / ToonsHub / VARYG / Trix multi-sub release of the same episode on Nyaa
     (`nyaa_arabic`, using `torrent.py`);
   - converts it to the Cairo ASS style with **`srt2ass.py`** (Cairo 82 @ 1920×1080, RTL marks,
     speaker dashes fixed, translator credit cue removed);
   - checks the timing against the episode's own audio with **`audio_fit.py`**: a consistent
     offset is corrected, an inconsistent one blocks the mux and asks for a look;
   - muxes in place with **`mux.py`** (MKVToolNix): Arabic named `العربية`, the only default
     subtitle, first among subs, Cairo fonts attached, every other track kept;
   - re-extracts the embedded track and verifies it matches what was built;
   - **copies** the finished episode into the Jellyfin *Weekly Anime* library
     (`copy_to_library`), see below;
   - shows a Windows notification.
3. The local status page shows live progress; no separate progress window opens.

A `manual` show without Arabic audio is skipped: it is typeset by hand (signs covered
and translated). An Arabic-dubbed episode of such a show bypasses subtitle processing.

## Jellyfin

Finished episodes are copied (the OneDrive copy stays) to
`C:\Users\moody\Downloads\Weekly Anime\<Show (Year)>\Season NN\`, which is the Jellyfin
library **Weekly Anime**. The `SHOWS` map in `agent.py` turns release names into folder names:

| Release name starts with | Folder |
|---|---|
| `<Release Title>` | `<English Title (Year)>` |

To follow a new show, add a line to `SHOWS` (English title + year, so Jellyfin finds its
metadata). A show not in the map gets a folder named after its release title.

## Status page, alerts, cleanup

### Pick downloads from Stremio

Open **Find movies & shows** in MyAnimeWatcher, or visit http://localhost:8097/stremio.
Your connected Stremio add-ons provide search results and available releases, including
the sources combined by AIOStreams. **Sync Stremio add-ons** refreshes the installed
add-on list from your account without changing that account.
Sync also refreshes live manifests and clears cached availability. Sources added inside
AIOStreams are returned through the existing AIOStreams connection; they are not separate
top-level Stremio add-ons in the Connected list.

Search globally across movies, series, and anime; results are grouped into those
three sections. Pick a title and episode, then filter available releases
by audio language, resolution and HDR (including Dolby Vision, HDR10/HDR10+, HLG, SDR, or unspecified
when reported). 10-bit alone does not mean HDR. **Check tracks** reads the selected source
with ffprobe before showing
its actual embedded audio and subtitle choices. Language codes are shown as unspecified
when the file has no language tag. **Find add-on subtitles** loads external subtitle
providers separately, so embedded track selection never waits for them. These subtitles may need
timing adjustment; their availability does not prove synchronization with that release.
Resolution and HDR filter labels are reported by the add-on/filename; verified pixel
dimensions and HDR metadata are shown after checking. Unavailable sources can be
replaced by another release. Missing HDR metadata is not assumed to be SDR.

Provider error videos are not valid downloads. The agent resolves redirect headers before
probing, rejects known AIOStreams error-video filenames and explicit provider errors,
and checks suspicious short clips against the advertised runtime/file size both before
and after downloading. Rejected clips are never published to Jellyfin or announced ready;
batch jobs try another matching source. Short videos with consistent metadata remain allowed.
Batch queues retain the catalog runtime for the same short-clip checks as individual downloads.

Typing at least two characters opens up to eight title suggestions after a short pause.
Use the arrow keys and Enter, or click a title; Escape dismisses the suggestions.
Searches, title details, release lists, and track checks show animated loading skeletons
(static when reduced motion is enabled). Series open on the first regular season rather
than Specials when both exist.

The main audio filter uses explicit audio metadata or source audio labels, including
AIOStreams' spoken-language flags. Subtitle languages and generic filename language tags
are not counted as audio. Unspecified languages remain separately selectable. Checking
a release updates its audio labels from the actual media tracks. Bidooo describes its
own sources as Arabic dubbed, so its returned streams are marked Arabic (reported)
until inspected. A catalog entry alone does not establish that a dubbed stream exists.
Bidooo's catalog is searched locally because it has no server-side search endpoint;
the release list reports when Bidooo returns no streams for the selected episode.

Availability results and media inspections are reused for five minutes. Slow add-ons
do not block the whole page; **Refresh releases** includes late responses without
repeating requests that are still running.

Choose the audio track, subtitles (or none), and an existing Jellyfin library, then
**Download to Jellyfin**. One manual download runs at a time, with progress on this page.
Progress includes a numeric percentage and an estimated time remaining, calculated
from elapsed time and processed media duration; slow/variable sources can change the
estimate. Final file verification is shown separately.
The selected tracks are copied into MKV without re-encoding the video. Incomplete files
use a `.part` extension; only verified files are renamed to `.mkv`. Existing movies and
episodes are never overwritten. Movies default to Movies, and series/anime default to
Anime; Weekly Anime retains its existing watched-episode cleanup policy. This manual
path now requires the same Arabic subtitle preparation and timing pipeline before publication.
The selected audio and additional subtitle tracks are retained; available embedded Arabic text
tracks are retained even when no additional subtitles are selected. Non-Arabic audio requires
verified Cairo Arabic subtitles. Arabic dubs keep the original audio and bypass subtitle work.

For a series/anime, **Download selected season** and **Download whole show** use the
settings picked via Check tracks. These buttons are visible directly below the
season/episode selectors and enable once a release and its tracks have been checked.
The chosen audio/subtitle languages, track roles (commentary, forced and hearing-impaired),
and the checked release's actual resolution/HDR are matched for each episode.
Only episodes already released in the catalog are queued. Episodes run sequentially;
existing files are skipped, and unavailable or mismatched sources are reported rather
than replaced with a different language/quality. Audio/subtitle choices need language
tags for batch matching. A missing language tag cannot be verified by listening.
If a matching source fails during download or verification, the batch tries another matching
release. Local save failures pause the queue. Each download has a total time limit: eight
times its runtime, with a minimum of one hour and a maximum of 24 hours; unknown runtimes
use one hour. A timed-out source is terminated and another matching release can be tried.

**Stop after current episode** leaves the remaining queue intact. **Resume / retry
remaining** continues pending episodes and retries failures/unavailable matches.
`stremio-batch.json` saves the queue without private stream URLs. After an app restart,
resume the interrupted queue explicitly; an interrupted episode restarts its download.
Disk shortages pause the queue. Whole-show downloads include released Specials (season
0) as well as regular seasons when the catalog provides them. Individual downloads
remain separate from the saved batch queue.

### Mandatory Arabic processing for all imports

All Stremio movies, series, anime and season/show downloads pass through `agent.process` in an
isolated `processing_worker.py` before they are published to the selected Jellyfin library.
Arabic text is taken from embedded tracks or a tagged `.ar.srt` / `.ara.srt` sidecar, searched
through connected Stremio subtitle add-ons, then the existing multi-sub Nyaa fallback. The
agent finds existing Arabic subtitles; it does not invent or machine-translate missing dialogue.
It applies the Cairo style, checks timing, muxes and re-extracts the result to verify it.

`library_processing.py` also checks all configured Jellyfin library paths recursively every
30 seconds. New and existing manual imports are processed after their size/mtime has stayed
unchanged for at least 60 seconds. MKV, MP4, M4V, AVI, MOV and WebM are supported. Weekly inbox
copies retain their existing worker. Library imports are processed in an isolated staging copy;
the original stays intact until the result passes. A byte-exact original is retained outside
Jellyfin in `work/library-processing/<job>/original.*` before replacement. Non-MKV inputs are
published as MKV after preparation; their original container is kept in that backup.

`library-processing.json` persists destinations, progress, input fingerprints and retry state;
it contains no private stream URLs. Raw Stremio downloads are retained outside Jellyfin while
waiting for Arabic or a successful check. Missing subtitles retry hourly, other processing or
delivery errors retry after 20 minutes, and pending items are shown in the dashboard. A season
can keep downloading other episodes while those items wait; it counts them separately and marks
them ready only when processing and delivery complete. No ready notification fires for a pending
item. Completed staging media is removed; manual original backups remain available for recovery.

Existing Arabic-dubbed manual files are left unchanged. Non-dubbed `manual` shows still require manual
typesetting and stays pending rather than being silently published by this new import path.

Track choices open directly under the checked release. The episode checklist in that panel
lets you choose exact episodes from the current season, then **Download selected episodes**.
Select all and Clear change the checkboxes; **Uncheck fillers** removes only episodes labelled
pure Filler. The full-season and whole-show buttons still download their entire scope.

Filler labels use AnimeFillerList guides for any matching anime, with an exact title match
and unique episode air-date matches within one broadcast day. Canon, mixed canon/filler and pure filler stay distinct.
Missing guides, ambiguous dates, Specials and unmatched episodes remain unclassified. The
guide loads separately from releases, is cached for 24 hours, and its source is linked in
the checklist. These community classifications do not guarantee that an episode is skippable.

Direct HTTP sources are supported. Torrent sources with a file index use the running
local Stremio service; external-player-only sources cannot be downloaded. Keep Stremio
Service running for torrent sources. Interrupted downloads must be selected again;
there is no automatic resume for manual downloads.

The connection is stored in `stremio-account.bin`, encrypted for your Windows user.
Private configured add-on URLs and account keys stay in the backend. If you change
Stremio accounts, run `python stremio_account.py --import-local` and reload the watcher.
The read-only storage importer in `work\stremio-storage-reader` and its Snappy helper
are needed only when importing, not during normal use. Keep their license files.

`python test_stremio.py` checks encrypted storage, input validation, and selected-track
downloads using a one-second local test video; it does not write to a real library.
Run tests with the agent's own interpreter (`C:\Users\moody\AppData\Local\Programs\Python\Python313\python.exe`,
packages in `requirements.txt`; see `AGENTS.md`):
`python -m unittest test_agent test_stremio test_stremio_batch test_dkb test_audit test_audit_fixes test_fansub test_fillers test_mandatory_processing test_robust test_reliability` checks the
dub safeguard and queue behavior as well; `test_audit` covers the 2026-11-15 audit fixes (single-file
runs, retries, copy failures, cleanup order, muxing, the status server and the egress proxy); `test_reliability`
covers the 2026-11-18 reliability audit (Windows file locks under status polling, verified-only mux swaps, proxy
idle timeouts and the Stremio local-server lockdown, cancel/pause/time limits with real ffmpeg, restarts, retry
back-off, Windows-1256 subtitles, stable library ids). `node test_picker.cjs` checks the picker
interaction handlers without installing browser dependencies.

- **MyAnimeWatcher app** (Start menu / Desktop shortcut, made by `make_shortcuts.ps1`): opens
  http://localhost:8097 as its own window (Edge app mode, icon `app.ico`): live status, served by the watcher (`status.py`). It shows the
  current job and progress, whether the supervisor, watcher and agent are running, the queue
  and inbox, every show with its library episode count, recent finishes, problems, scheduled
  deletions and the log. It refreshes every 2 s.
- **Quiet by default:** no progress window, no command windows. The only pop-up is the
  Windows notification **"New episode ready"** or **"New movie ready"** (from MyAnimeWatcher), according to
  the content type, when the finished item is in
  Jellyfin. Every other message (waiting, needs a look, errors) goes to the log and the page.
- **Auto-cleanup** (`cleanup.py`, hourly): an episode in Weekly Anime or Anime that Jellyfin marks fully watched is sent to the **Recycle Bin** 24 h later; for Weekly
  Anime its OneDrive inbox copy goes too. The Movies library is never touched.
  `python cleanup.py --dry` lists what is scheduled.

## How it stays running

- Task Scheduler task **"Kizuna"** starts `watcher.py` at logon and unlock.
- `watcher.py` runs as a supervisor that relaunches the real watcher (`--child`) within 10 s
  if it dies; named mutexes keep a single copy of each.
- Transient errors retry every 20 min (3 strikes); episodes without Arabic yet are retried for
  up to 2 days. Files that arrived while the PC was off are picked up at logon.
- The watcher runs `agent.py <that one file> --now`, so only the episode that finished syncing is
  touched; other episodes keep their own retry schedule and their own strike count. A strike count is
  cleared once a file is marked failed (a replaced file gets 3 fresh tries), and records of deleted
  files are dropped.
- If an agent run exceeds 3 h it is stopped together with every program it started (mkvmerge,
  ffmpeg, torrent.py) and the episode is retried 20 min later.
- "New episode ready" only fires once the episode is really in Jellyfin. A failed Jellyfin copy is
  recorded (`copy_failed` in `state.json`), shown under "Needs a look", and retried by the watcher;
  the alert fires when the retry succeeds. A failed final check is also shown under "Needs a look".
- Before muxing, the agent saves a fingerprint of the intended subtitle dialogue. After a restart,
  an interrupted delivery re-extracts and verifies that subtitle before copying it to Jellyfin.
  Silent audio, failed audio decoding and weak timing measurements block automatic timing changes.

## Network safety

- `egress.py` is a small local filtering proxy. Every connection ffmpeg/ffprobe and the Stremio
  link checks make goes through it: it resolves each host itself, refuses anything that is not a
  public address, and connects to exactly the address it checked. Redirects, HLS playlists and
  segments, encryption keys and DNS changes are all covered. The one local exception is Stremio's
  torrent server `127.0.0.1:11470`. Media links may no longer point at Jellyfin (`:8096`).
- Search-result posters must be `https` with a real host name (no IP addresses or LAN names).
- The status app only answers requests addressed to `localhost`/`127.0.0.1` on its own port.
- `torrent.py` no longer asks the router to open ports (UPnP/NAT-PMP off).

## Files

| File | Purpose |
|---|---|
| `agent.py` | the pipeline above |
| `library_processing.py` | durable mandatory preparation queue and recursive library monitoring |
| `processing_worker.py` | runs the shared Arabic pipeline in an isolated staging folder |
| `library-processing.json` | import progress, source fingerprints, publication and retry records |
| `watcher.py` | folder watch + supervisor |
| `dkb.py` | checks DKB and downloads new episodes for the followed shows |
| `dkb-state.json` | initial episode baselines and completed DKB downloads |
| `srt2ass.py` | SRT → styled Cairo ASS |
| `mux.py` | safe in-place mux (`.tmp` + verify + swap) |
| `audio_fit.py` | timing check against the audio |
| `torrent.py` | libtorrent downloads for new DKB episodes and Arabic-track fallback |
| `fonts\` | Cairo Bold / SemiBold attached to every episode |
| `log.txt` | everything the agent did — check here first |
| `state.json` | finished / failed episodes |
| `work\` | scratch files |

## Run by hand

```
python agent.py                     # process the default folder now
python agent.py "D:\some folder"    # process another folder
```
