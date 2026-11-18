"""Weekly shows: the private list, add/edit/remove, linking, and DKB + Stremio as competing sources.
Temp folders only; AniList, DKB and Stremio are simulated."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

import agent
import cleanup
import dkb
import library_processing as lp
import processing_worker as worker
import shows
import stremio as s
import stremio_batch as b
from test_audit import Sandbox


def show(**kw):
    base = {"release": "Kaiju Squad", "folder": "Kaiju Squad (2024)", "season": 2, "anilist": 111, "dkb": True}
    return dict(base, **kw)


class ShowsSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        self.anime_lib = self.anime
        import settings
        settings._cache.update(stamp="reset", data=dict(settings.DEFAULTS), damaged=False)
        self.extra = [mock.patch.object(shows, "FILE", Path(self.tmp, "shows.json")),
                      mock.patch.object(shows, "STATE", Path(self.tmp, "weekly-state.json")),
                      mock.patch.object(shows, "RESETS", Path(self.tmp, "shows-pending.json")),
                      mock.patch.object(settings, "FILE", Path(self.tmp, "settings.json")),
                      mock.patch.object(lp, "FILE", Path(self.tmp, "queue.json")),
                      mock.patch.object(lp, "ROOT", Path(self.tmp, "q"))]
        for p in self.extra:
            p.start()
        shows._claims.clear()
        for pending in shows._resets.values():
            pending.clear()

    def tearDown(self):
        for p in self.extra:
            p.stop()
        shows._claims.clear()
        super().tearDown()

    def follow(self, *items):
        shows.save(list(items))


# ---------------------------------------------------------------- the list
class List(ShowsSandbox):
    def test_agent_sees_page_edits_without_restart(self):
        self.assertEqual(dict(agent.SHOWS), {})
        self.follow(show())
        self.assertEqual(agent.SHOWS["Kaiju Squad"], "Kaiju Squad (2024)")
        target = agent.library_target("[DKB] Kaiju Squad - S02E03 [1080p].mkv")
        self.assertEqual(target, os.path.join(self.lib, "Kaiju Squad (2024)", "Season 02", "[DKB] Kaiju Squad - S02E03 [1080p].mkv"))
        self.follow(show(folder="Kaiju Squad Renamed (2024)"))
        self.assertEqual(agent.SHOWS["Kaiju Squad"], "Kaiju Squad Renamed (2024)")

    def test_match_prefers_the_longest_release_name(self):
        self.follow(show(release="Kaiju"), show(release="Kaiju Squad", folder="Other (2020)"))
        self.assertEqual(shows.match("Kaiju Squad")["folder"], "Other (2020)")
        self.assertEqual(shows.match("Kaiju")["folder"], "Kaiju Squad (2024)")
        self.assertIsNone(shows.match("Kaijuu"))

    def test_policy_and_dkb_names(self):
        self.follow(show(policy="manual"), show(release="Moon", folder="Moon (2025)", dkb=False))
        import fansub
        self.assertEqual(fansub.mode_for("Kaiju Squad"), "manual")
        self.assertEqual(fansub.mode_for("Kaiju Squad (2024)"), "manual")
        self.assertEqual(shows.dkb_names(), ("Kaiju Squad",))

    def test_manual_show_is_held_by_the_weekly_agent(self):
        self.follow(show(policy="manual"))
        v = self.episode("[DKB] Kaiju Squad - S02E03 [1080p].mkv")
        with mock.patch.object(agent, "tracks", return_value=[]), mock.patch.object(agent, "process") as process, \
                mock.patch.object(agent, "TARGET", v), mock.patch.object(agent, "NOW", True):
            agent.main()
        process.assert_not_called()
        self.assertEqual(self.toasts, ["Kizuna: manual"])


class AddEditRemove(ShowsSandbox):
    def form(self, **kw):
        base = {"release": "Kaiju Squad", "folder": "Kaiju Squad (2024)", "season": "2", "anilist": "111",
                "dkb": "true", "policy": "fansub_first"}
        return dict(base, **kw)

    def test_add_creates_the_library_folder_and_wakes_both_sources(self):
        shows.wake_dkb.clear()
        shows.wake_stremio.clear()
        added = shows.add(self.form(stremio_id="tt123", stremio_season="5", stremio_offset="-60", from_="",
                                    poster="https://s4.anilist.co/file/x.jpg"))
        self.assertTrue(Path(self.lib, "Kaiju Squad (2024)").is_dir())
        self.assertEqual(added["stremio"], {"id": "tt123", "type": "series", "season": 5, "offset": -60, "on": True})
        self.assertEqual(added["poster"], "https://s4.anilist.co/file/x.jpg")
        self.assertTrue(shows.wake_dkb.is_set() and shows.wake_stremio.is_set())

    def test_bad_input_and_duplicates_are_refused(self):
        shows.add(self.form())
        for bad in (self.form(release="Other", folder="Kaiju Squad (2024)"), self.form(folder="New (2020)"),
                    self.form(release="x"), self.form(release="A/B"), self.form(release="Other", folder="CON"),
                    self.form(release="Other", folder="Other", season="abc"), self.form(release="Other", folder="O2", stremio_id="bad id!")):
            with self.assertRaises(shows.ShowError):
                shows.add(bad)
        self.assertNotIn("poster", shows.add(self.form(release="Other", folder="O3", poster="https://evil.example/x.jpg")))

    def test_edit_renames_the_folder_with_its_episodes(self):
        shows.add(self.form())
        ep = self.episode("Kaiju Squad - S02E01.mkv", folder=os.path.join(self.lib, "Kaiju Squad (2024)", "Season 02"))
        shows.edit(self.form(original="Kaiju Squad", folder="Kaiju Squad X (2024)"))
        self.assertFalse(os.path.exists(ep))
        self.assertTrue(Path(self.lib, "Kaiju Squad X (2024)", "Season 02", "Kaiju Squad - S02E01.mkv").exists())
        Path(self.lib, "Taken (2020)").mkdir()
        with self.assertRaises(shows.ShowError):
            shows.edit(self.form(original="Kaiju Squad", folder="Taken (2020)"))

    def test_failed_rename_leaves_nothing_pending(self):
        shows.add(self.form())
        Path(self.lib, "Taken (2020)").mkdir()
        with self.assertRaises(shows.ShowError):
            shows.edit(self.form(original="Kaiju Squad", release="Kaiju Renamed", folder="Taken (2020)"))
        self.assertFalse(shows.RESETS.exists() and json.loads(shows.RESETS.read_text()).get("dkb"))
        self.assertEqual(shows.take_resets("dkb"), {})
        with mock.patch.object(Path, "rename", side_effect=PermissionError("in use")):
            with self.assertRaises(shows.ShowError):
                shows.edit(self.form(original="Kaiju Squad", release="Kaiju Renamed", folder="Other (2020)"))
        self.assertEqual(shows.take_resets("stremio"), {})
        self.assertEqual(shows.load()[0]["release"], "Kaiju Squad")

    def test_name_change_refused_while_an_episode_is_in_flight(self):
        shows.add(self.form())
        shows.claim("Kaiju Squad", "S02E05", "stremio")
        with self.assertRaises(shows.ShowError):
            shows.edit(self.form(original="Kaiju Squad", release="Kaiju Renamed"))
        shows.edit(self.form(original="Kaiju Squad", policy="manual"))           # other edits still fine

    def test_remove_moves_episodes_to_the_anime_library_and_never_deletes(self):
        shows.add(self.form())
        self.episode("Kaiju Squad - S02E01.mkv", folder=os.path.join(self.lib, "Kaiju Squad (2024)", "Season 02"))
        result = shows.remove("Kaiju Squad")
        self.assertIn("moved", result["note"])
        self.assertTrue(Path(self.anime, "Kaiju Squad (2024)", "Season 02", "Kaiju Squad - S02E01.mkv").exists())
        self.assertEqual(shows.load(), [])

    def test_remove_keeps_the_folder_when_the_anime_library_has_one(self):
        shows.add(self.form())
        ep = self.episode("Kaiju Squad - S02E01.mkv", folder=os.path.join(self.lib, "Kaiju Squad (2024)", "Season 02"))
        Path(self.anime, "Kaiju Squad (2024)").mkdir()
        self.assertIn("left in place", shows.remove("Kaiju Squad")["note"])
        self.assertTrue(os.path.exists(ep))


# ---------------------------------------------------------------- one episode, one source
class OneSource(ShowsSandbox):
    def test_claims_are_exclusive_between_sources(self):
        self.assertTrue(shows.claim("Kaiju Squad", "S02E03", "dkb"))
        self.assertTrue(shows.claim("Kaiju Squad", "S02E03", "dkb"))
        self.assertFalse(shows.claim("Kaiju Squad", "S02E03", "stremio"))
        shows.release_claim("Kaiju Squad", "S02E03")
        self.assertTrue(shows.claim("Kaiju Squad", "S02E03", "stremio"))

    def test_have_episode_sees_every_place_an_episode_can_be(self):
        self.follow(show())
        sh = shows.load()[0]
        self.assertFalse(shows.have_episode(sh, "S02E03"))
        p = self.episode("Kaiju Squad (2024) - S02E03.mkv", folder=os.path.join(self.lib, "Kaiju Squad (2024)", "Season 02"))
        self.assertTrue(shows.have_episode(sh, "S02E03"))
        self.assertFalse(shows.have_episode(sh, "S02E30"))
        os.remove(p)
        self.episode("[DKB] Kaiju Squad - S02E04 [1080p].mkv")                   # inbox
        self.assertTrue(shows.have_episode(sh, "S02E04"))
        dest = Path(self.lib, "Kaiju Squad (2024)", "Season 02", "Kaiju Squad (2024) - S02E05.mkv")
        lp.save_record("k", {"status": "waiting", "destination": str(dest)})      # downloaded, waiting for Arabic
        self.assertTrue(shows.have_episode(sh, "S02E05"))
        Path(self.tmp, "dkb-state.json").write_text(json.dumps({"done": {"Kaiju Squad|S02E06": "x.mkv"}}))
        self.assertTrue(shows.have_episode(sh, "S02E06"))

    def test_dkb_skips_an_episode_stremio_already_brought(self):
        self.follow(show())
        Path(self.lib, "Kaiju Squad (2024)", "Season 02").mkdir(parents=True)
        Path(self.lib, "Kaiju Squad (2024)", "Season 02", "Kaiju Squad (2024) - S02E06.mkv").write_bytes(b"x")
        title = "[DKB] Kaiju Squad - S02E06 [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs][weekly]"
        with mock.patch.object(dkb, "STATE", os.path.join(self.tmp, "dkb-state.json")), \
                mock.patch.object(dkb, "listing", return_value=[(title, "https://nyaa.si/download/1.torrent")]), \
                mock.patch.object(dkb, "download") as download:
            Path(dkb.STATE).write_text(json.dumps({"baseline": {"Kaiju Squad": "S02E05"}, "done": {}}))
            state = dkb.poll(self.inbox, lambda p: None)
        download.assert_not_called()
        self.assertIn("Kaiju Squad|S02E06", state["done"])                       # recorded, never downloaded

    def test_dkb_waits_while_stremio_is_fetching_the_same_episode(self):
        self.follow(show())
        title = "[DKB] Kaiju Squad - S02E06 [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs][weekly]"
        shows.claim("Kaiju Squad", "S02E06", "stremio")
        with mock.patch.object(dkb, "STATE", os.path.join(self.tmp, "dkb-state.json")), \
                mock.patch.object(dkb, "listing", return_value=[(title, "https://nyaa.si/download/1.torrent")]), \
                mock.patch.object(dkb, "download") as download:
            Path(dkb.STATE).write_text(json.dumps({"baseline": {"Kaiju Squad": "S02E05"}, "done": {}}))
            state = dkb.poll(self.inbox, lambda p: None)
        download.assert_not_called()
        self.assertEqual(state["done"], {})                                       # retried next check

    def test_dkb_start_episode_from_the_page(self):
        self.follow(show(**{"from": 4}))
        titles = [(f"[DKB] Kaiju Squad - S02E0{n} [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs][weekly]",
                   "https://nyaa.si/download/1.torrent") for n in (3, 4, 5)]
        with mock.patch.object(dkb, "STATE", os.path.join(self.tmp, "dkb-state.json")), \
                mock.patch.object(dkb, "listing", return_value=titles), \
                mock.patch.object(dkb, "download", side_effect=lambda t, u, f: os.path.join(self.tmp, t[:30] + ".mkv")) as download:
            dkb.poll(self.inbox, lambda p: None)
        self.assertEqual([dkb.episode(c.args[0])[1] for c in download.call_args_list], ["S02E04", "S02E05"])


# ---------------------------------------------------------------- Stremio as the second source
class StremioWeekly(ShowsSandbox):
    def setUp(self):
        super().setUp()
        self.follow(show(stremio={"id": "tt9", "type": "series", "season": 5, "offset": -60, "on": True}))

    def aired(self, numbers):
        return [(shows.tag(2, n + -60), n - 60, {"id": f"tt9:5:{n}", "season": 5, "episode": n}, {"id": "tt9"})
                for n in numbers]

    def poll(self, numbers, job=None, **kw):
        with mock.patch.object(shows, "aired", return_value=self.aired(numbers)), \
                mock.patch.object(shows, "fetch", return_value=job or {"status": "done", "stage": "ok"}, **kw) as fetch:
            state = shows.stremio_poll()
        return state, fetch

    def test_first_check_sets_a_baseline_without_backfill(self):
        state, fetch = self.poll([61, 62, 63])
        fetch.assert_not_called()
        self.assertEqual(state["baseline"]["Kaiju Squad|S02"], 3)            # per season

    def test_new_episode_is_downloaded_once_with_library_numbering(self):
        self.poll([61, 62, 63])
        state, fetch = self.poll([61, 62, 63, 64])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[1:3], ("S02E04", 4))
        self.assertEqual(state["done"]["Kaiju Squad|S02E04"], "stremio")
        state, fetch = self.poll([61, 62, 63, 64])
        fetch.assert_not_called()

    def test_episode_dkb_already_brought_is_skipped(self):
        self.poll([61, 62, 63])
        self.episode("[DKB] Kaiju Squad - S02E04 [1080p].mkv")
        state, fetch = self.poll([61, 62, 63, 64])
        fetch.assert_not_called()
        self.assertEqual(state["done"]["Kaiju Squad|S02E04"], "already here")

    def test_waits_while_dkb_fetches_or_the_user_downloads(self):
        self.poll([61, 62, 63])
        shows.claim("Kaiju Squad", "S02E04", "dkb")
        _, fetch = self.poll([61, 62, 63, 64])
        fetch.assert_not_called()
        shows.release_claim("Kaiju Squad", "S02E04")
        s._job_lock.acquire()
        try:
            state, fetch = self.poll([61, 62, 63, 64])
        finally:
            s._job_lock.release()
        fetch.assert_not_called()
        self.assertNotIn("Kaiju Squad|S02E04", state["done"])

    def test_unavailable_episodes_keep_retrying_for_days_then_stop(self):
        self.poll([61, 62, 63])
        for _ in range(20):                                                       # ~7 hours of checks: still trying
            state, fetch = self.poll([61, 62, 63, 64], job={"status": "unavailable", "stage": "none"})
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("Kaiju Squad|S02E04", state["fails"])
        self.assertNotIn("Kaiju Squad|S02E04", state["skipped"])
        state["fails"]["Kaiju Squad|S02E04"]["first"] -= (shows.GIVE_UP_DAYS + 1) * 86400
        shows.write_state(state)
        state, fetch = self.poll([61, 62, 63, 64], job={"status": "unavailable", "stage": "none"})
        self.assertEqual(state["skipped"]["Kaiju Squad|S02E04"], "not found")
        state, fetch = self.poll([61, 62, 63, 64])
        fetch.assert_not_called()
        self.assertEqual(shows.overview()["shows"][0]["not_found"], ["S02E04"])     # shown on the page

    def test_cancel_stops_stremio_but_lets_dkb_bring_it(self):
        self.poll([61, 62, 63])
        state, _ = self.poll([61, 62, 63, 64], job={"status": "cancelled", "stage": "Download cancelled."})
        self.assertEqual(state["skipped"]["Kaiju Squad|S02E04"], "cancelled")
        _, fetch = self.poll([61, 62, 63, 64])
        fetch.assert_not_called()
        self.assertFalse(shows.have_episode(shows.load()[0], "S02E04"))           # DKB is not blocked

    def test_stopping_a_season_queue_does_not_stop_weekly_fetches(self):
        b._cancel.set()
        try:
            sh = shows.load()[0]
            library = {"id": "Weekly Anime:x", "name": "Weekly Anime", "path": self.lib}
            with mock.patch.object(s, "libraries", return_value=[library]), \
                    mock.patch.object(s, "streams", return_value={"sources": [], "failed_addons": []}) as streams, \
                    mock.patch.object(s, "remember", return_value="k"):
                shows.fetch(sh, "S02E04", 4, {"id": "tt9:5:64"}, {"id": "tt9"})
            self.assertTrue(streams.called)                                       # it really searched
        finally:
            b._cancel.clear()

    def test_empty_first_answer_never_starts_from_zero(self):
        self.poll([])
        self.assertNotIn("Kaiju Squad|S02", shows.read_state()["baseline"])
        _, fetch = self.poll([61, 62, 63])
        fetch.assert_not_called()                                                 # baseline taken now, no backfill

    def test_season_change_and_re_adding_start_fresh(self):
        self.poll([61, 62, 63])
        shows.request_reset("Kaiju Squad")
        state, fetch = self.poll([61, 62, 63, 64, 65])
        fetch.assert_not_called()
        self.assertEqual(state["baseline"]["Kaiju Squad|S02"], 5)

    def test_damaged_ledger_fails_closed_and_is_never_rewritten(self):
        self.poll([61, 62, 63])
        shows.STATE.write_text("{half a file", encoding="utf-8")
        with mock.patch.object(shows, "aired", return_value=self.aired([61, 62, 63, 64])), \
                mock.patch.object(shows, "fetch") as fetch:
            self.assertIsNone(shows.stremio_poll())
        fetch.assert_not_called()
        self.assertEqual(shows.STATE.read_text(encoding="utf-8"), "{half a file")   # left for the user to see
        self.assertEqual(len(shows.overview()["shows"]), 1)                          # the page still works

    def test_reset_survives_a_restart(self):
        self.poll([61, 62, 63])
        with mock.patch.object(shows, "RESETS", Path(self.tmp, "pending.json")):
            shows.request_reset("Kaiju Squad")
            for pending in shows._resets.values():
                pending.clear()                                                      # "restart": memory is gone
            state, fetch = self.poll([61, 62, 63, 64, 65])
            self.assertEqual(state["baseline"]["Kaiju Squad|S02"], 5)
            self.assertEqual(shows.take_resets("stremio"), {})

    def test_renaming_the_release_keeps_finished_episodes_finished(self):
        self.poll([61, 62, 63])
        self.poll([61, 62, 63, 64])                                                  # S02E04 fetched under the old name
        with mock.patch.object(shows, "RESETS", Path(self.tmp, "pending.json")):
            self.follow(show(release="Kaiju Squad TV", stremio={"id": "tt9", "type": "series", "season": 5, "offset": -60, "on": True},
                             **{"from": 1}))
            shows.request_reset("Kaiju Squad", renamed_to="Kaiju Squad TV")
            state, fetch = self.poll([61, 62, 63, 64])
        self.assertIn("Kaiju Squad TV|S02E04", state["done"])
        self.assertNotIn("S02E04", [c.args[1] for c in fetch.call_args_list])        # not fetched twice

    def test_rename_chains_collapse_and_back_and_forth_is_a_no_op(self):
        shows.request_reset("A", renamed_to="B")
        shows.request_reset("B", renamed_to="C")
        self.assertEqual(shows.take_resets("dkb"), {"A": "C", "B": "C"})
        shows.request_reset("C", renamed_to="A")
        self.assertEqual(shows.take_resets("dkb")["A"], "A")

    def test_unreadable_pending_file_is_never_wiped(self):
        shows.request_reset("A", renamed_to="B")
        before = shows.RESETS.read_text(encoding="utf-8")
        with mock.patch.object(shows, "_pending", side_effect=PermissionError("antivirus")):
            self.assertEqual(shows.take_resets("dkb"), {"A": "A"})                 # memory only
            shows.ack_resets("dkb", {"A": "A"})
        self.assertEqual(shows.RESETS.read_text(encoding="utf-8"), before)

    def test_pending_entries_stay_until_applied_and_saved(self):
        self.poll([61, 62, 63])
        shows.request_reset("Kaiju Squad")
        with mock.patch.object(shows, "write_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.poll([61, 62, 63, 64])
        self.assertIn("Kaiju Squad", shows.take_resets("stremio"))                  # still there after the crash
        self.poll([61, 62, 63, 64])
        self.assertEqual(shows.take_resets("stremio"), {})

    def test_busy_user_is_not_a_failed_attempt(self):
        self.poll([61, 62, 63])
        with mock.patch.object(shows, "aired", return_value=self.aired([61, 62, 63, 64])), \
                mock.patch.object(shows, "fetch", side_effect=shows.Busy()):
            state = shows.stremio_poll()
        self.assertNotIn("Kaiju Squad|S02E04", state["fails"])

    def test_start_episode_backfills_from_there(self):
        self.follow(show(**{"from": 2}, stremio={"id": "tt9", "type": "series", "season": 5, "offset": -60, "on": True}))
        state, fetch = self.poll([61, 62, 63])
        self.assertEqual([c.args[1] for c in fetch.call_args_list], ["S02E02", "S02E03"])

    def test_fetch_downloads_into_the_shows_weekly_folder(self):
        sh = shows.load()[0]
        library = {"id": "Weekly Anime:x", "name": "Weekly Anime", "path": self.lib}
        with mock.patch.object(s, "libraries", return_value=[library]), \
                mock.patch.object(b, "choose", side_effect=[None, ("key", "1", "none")]) as choose, \
                mock.patch.object(s, "_job_lock", __import__("threading").Lock()), \
                mock.patch.object(s, "start_download", return_value={"status": "waiting", "stage": "Arabic"}) as start:
            job = shows.fetch(sh, "S02E04", 4, {"id": "tt9:5:64"}, {"id": "tt9", "runtime": "24 min"})
        self.assertEqual(job["status"], "waiting")
        video = choose.call_args.args[1]
        self.assertEqual((video["id"], video["season"], video["episode"]), ("tt9:5:64", 2, 4))
        self.assertEqual(s.target(video, library), Path(self.lib).resolve() / "Kaiju Squad (2024)" / "Season 02" / "Kaiju Squad (2024) - S02E04.mkv")
        self.assertEqual([c.args[0]["preferences"]["resolution"] for c in choose.call_args_list], ["1080p", "720p"])
        start.assert_called_once_with("key", "1", "none", "Weekly Anime:x", _batch=True)

    def test_subtitle_addons_get_the_catalog_numbering(self):
        md = {"type": "series", "id": "tt9:5:64", "season": 2, "episode": 4, "meta": {"id": "tt9", "name": "Kaiju Squad"}}
        with mock.patch.object(s, "get_json", return_value={"meta": {"name": "Kaiju Squad"}}), \
                mock.patch.object(worker.fansub, "pick", return_value="Arabic") as pick:
            worker.fetch_arabic(md, "out.srt", "official_ok", "media.mkv")
        self.assertEqual(pick.call_args.args[1:4], ("tt9", 5, 64))


# ---------------------------------------------------------------- AniList, linking
class Linking(ShowsSandbox):
    def media(self, mid, romaji, english, year, nxt=None, prequel=None):
        return {"id": mid, "title": {"romaji": romaji, "english": english}, "synonyms": [], "format": "TV",
                "season": "FALL", "seasonYear": year, "episodes": 12, "status": "RELEASING",
                "nextAiringEpisode": {"episode": nxt, "airingAt": 2_000_000_000} if nxt else None,
                "coverImage": {"large": "https://s4.anilist.co/c.jpg"}, "startDate": {"year": year},
                "relations": {"edges": [{"relationType": "PREQUEL", "node": {"id": prequel, "format": "TV", "title": {"romaji": romaji}}}] if prequel else []}}

    def test_library_shows_get_their_jellyfin_titles_and_posters(self):
        import contextlib
        rows = [("AB-12", "بلو بوكس", "アオのハコ", r"C:\lib\Blue Box (2024)", "نبذة")]
        @contextlib.contextmanager
        def db():
            yield mock.Mock(execute=lambda *a: mock.Mock(fetchall=lambda: rows))
        shows._local.update(t=0, data=None)
        with mock.patch.object(agent, "jellyfin_db", db):
            self.assertEqual(shows.local_for(folder="Blue Box (2024)")["title"], "بلو بوكس")
            self.assertEqual(shows.local_for(native="アオのハコ")["about"], "نبذة")
            self.assertIn("/Items/ab12/Images/Primary", shows.local_for(folder="blue box (2024)")["poster"])
            self.assertIsNone(shows.local_for(folder="Other"))
        shows._local.update(t=0, data=None)

    def test_weekly_episodes_get_filler_labels_by_air_date(self):
        import fillers
        guide_list = mock.Mock(links=[("Kaiju Squad", "/shows/kaiju-squad"), ("Other Show", "/shows/other")])
        guide = mock.Mock(rows=[["101", "Ep", "Canon", "2026-11-01"], ["102", "Ep", "Filler", "2026-11-08"],
                                ["103", "Ep", "Mixed Canon/Filler", "2026-11-15"]])
        day = lambda d: int(datetime(2026, 11, d, 15, tzinfo=timezone.utc).timestamp())      # noqa: E731
        with mock.patch.object(fillers, "page", side_effect=lambda p: guide_list if p == "/shows" else guide):
            labels = fillers.schedule_labels(["Kaiju Squad (2024)", "Kaiju Squad"], {5: day(1), 6: day(8), 7: day(15), 8: day(22)})
            self.assertEqual(labels, {5: "Canon", 6: "Filler", 7: "Mixed canon/filler"})
            self.assertEqual(fillers.schedule_labels(["Unknown"], {1: day(1)}), {})
        import status
        self.follow(show())
        with mock.patch.dict(status._fill, {"t": time.time(), "data": {"Kaiju Squad": labels}, "busy": False}):
            self.assertEqual(status._filler_for("[DKB] Kaiju Squad - S02E06 [1080p].mkv"), "Filler")
            self.assertEqual(status._filler_for("[DKB] Kaiju Squad - S02E05 [1080p].mkv"), "")      # canon: unmarked
            self.assertEqual(status._filler_for("[DKB] Kaiju Squad - S01E06 [1080p].mkv"), "")      # other season
            self.assertEqual(status._filler_for("Something else.mkv"), "")

    def test_season_boundaries(self):
        at = lambda y, m: datetime(y, m, 15, tzinfo=timezone.utc).timestamp()      # noqa: E731
        self.assertEqual(shows.current_season(at(2026, 11)), ("FALL", 2026))
        self.assertEqual(shows.current_season(at(2026, 12)), ("WINTER", 2027))
        self.assertEqual(shows.current_season(at(2027, 1)), ("WINTER", 2027))
        self.assertEqual(shows.current_season(at(2027, 4)), ("SPRING", 2027))

    def test_link_fills_folder_season_and_both_sources(self):
        s2 = self.media(222, "Kaiju Squad 2nd Season", "Kaiju Squad Season 2", 2026, nxt=7, prequel=111)
        s1 = self.media(111, "Kaiju Squad", "Kaiju Squad", 2024)
        answers = {222: s2, 111: s1}
        with mock.patch.object(shows, "anilist", side_effect=lambda q, v: {"Media": answers[v["id"]]}), \
                mock.patch.object(dkb, "search_titles", return_value=[
                    "[DKB] Kaiju Squad S2 - S02E05 [1080p]", "[DKB] Kaiju Squad S2 - S02E06 [1080p]"]), \
                mock.patch.object(shows, "stremio_lookup", return_value={"id": "tt9", "type": "series", "name": "Kaiju Squad",
                                                                         "season": 5, "latest": 66}):
            linked = shows.link(222)
        self.assertEqual(linked["folder"], "Kaiju Squad (2024)")
        self.assertEqual((linked["season"], linked["release"]), (2, "Kaiju Squad S2"))
        self.assertEqual(linked["dkb"], {"release": "Kaiju Squad S2", "season": 2, "latest": 6})
        self.assertEqual(linked["stremio"]["offset"], 6 - 66)                     # library ep = Stremio ep - 60

    def test_link_without_dkb_uses_stremio_numbering(self):
        s1 = self.media(111, "Moon Hunters", None, 2026, nxt=3)
        with mock.patch.object(shows, "anilist", return_value={"Media": s1}), \
                mock.patch.object(dkb, "search_titles", return_value=[]), \
                mock.patch.object(shows, "stremio_lookup", return_value={"id": "tt5", "type": "series", "name": "Moon Hunters",
                                                                         "season": 1, "latest": 2}):
            linked = shows.link(111)
        self.assertIsNone(linked["dkb"])
        self.assertEqual((linked["folder"], linked["season"], linked["stremio"]["offset"]), ("Moon Hunters (2026)", 1, 0))

    def test_prequels_from_other_series_are_not_followed(self):
        self.assertTrue(shows.same_series("Kusuriya no Hitorigoto 3rd Season", "Kusuriya no Hitorigoto"))
        self.assertFalse(shows.same_series("ONE PIECE", "MONSTERS: Ippaku Sanjou Hiryuu Jigoku"))
        current = self.media(21, "ONE PIECE", "ONE PIECE", 1999, nxt=1181)
        current["relations"]["edges"] = [{"relationType": "PREQUEL",
                                          "node": {"id": 99, "format": "ONA", "title": {"romaji": "MONSTERS"}}}]
        with mock.patch.object(shows, "anilist", return_value={"Media": current}) as call:
            _, first, number = shows.series_root(21)
        self.assertEqual((first["id"], number, call.call_count), (21, 1, 1))

    def test_only_episodes_already_released_count(self):
        meta = {"videos": [{"id": "a", "season": 3, "episode": 7, "released": "2026-11-10T00:00:00Z"},
                           {"id": "b", "season": 3, "episode": 8, "released": "2099-01-01T00:00:00Z"},
                           {"id": "c", "season": 3, "episode": 24},                       # no date: not aired
                           {"id": "d", "season": 2, "episode": 24, "released": "2025-03-01T00:00:00Z"}]}
        now = datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp()
        self.assertEqual([e["id"] for e in shows.released(meta, 3, now)], ["a"])
        self.assertEqual([e["id"] for e in shows.released(meta, None, now)], ["d", "a"])

    def test_recommendations_put_new_shows_first_and_mark_followed(self):
        self.follow(show(anilist=2))
        data = {"new": {"media": [self.media(1, "A", "A en", 2026, nxt=3), self.media(2, "B", None, 2026)]},
                "old": {"media": [self.media(2, "B", None, 2026), self.media(3, "C", "C en", 2020, nxt=500)]}}
        data["old"]["media"][1]["coverImage"]["large"] = "https://evil.example/c.jpg"
        shows._recommend.update(data=None)
        with mock.patch.object(shows, "anilist", return_value=data):
            rec = shows.recommendations()
        self.assertEqual([(c["anilist"], c["new"], c["followed"]) for c in rec["shows"]],
                         [(1, True, False), (2, True, True), (3, False, False)])
        self.assertEqual(rec["shows"][2]["poster"], "")                           # only AniList's image host
        shows._recommend.update(data=None)


# ---------------------------------------------------------------- the page and its API
class Api(ShowsSandbox):
    def setUp(self):
        super().setUp()
        import status
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), status.make_handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, path, data=None, token=True):
        req = urllib.request.Request(self.base + path, method="POST" if data is not None else "GET",
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"X-MyAnimeWatcher": s.TOKEN} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null") if "api" in path else r.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, json.loads(body) if body.startswith(b"{") else None

    def test_page_and_changes_need_the_token(self):
        code, page = self.call("/weekly")
        self.assertEqual(code, 200)
        self.assertIn(s.TOKEN, page)
        self.assertNotIn("__STREMIO_TOKEN__", page)
        self.assertEqual(self.call("/api/stremio/shows-add", {"release": "Kaiju Squad"}, token=False)[0], 403)
        self.assertEqual(shows.load(), [])

    def test_add_list_edit_remove_through_the_api(self):
        form = {"release": "Kaiju Squad", "folder": "Kaiju Squad (2024)", "season": "2", "anilist": "111",
                "dkb": "true", "policy": "official_ok"}
        self.assertEqual(self.call("/api/stremio/shows-add", form)[0], 200)
        code, data = self.call("/api/stremio/shows")
        self.assertEqual([x["folder"] for x in data["shows"]], ["Kaiju Squad (2024)"])
        code, data = self.call("/api/stremio/shows-add", form)
        self.assertEqual((code, data["error"]), (400, "That release name is already followed."))
        self.assertEqual(self.call("/api/stremio/shows-edit", dict(form, original="Kaiju Squad", policy="manual"))[0], 200)
        self.assertEqual(shows.load()[0]["policy"], "manual")
        self.assertEqual(self.call("/api/stremio/shows-check", {})[0], 200)
        self.assertEqual(self.call("/api/stremio/shows-remove", {"release": "Kaiju Squad"})[0], 200)
        self.assertEqual(shows.load(), [])


if __name__ == "__main__":
    unittest.main()
