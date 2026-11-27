"""Family audit: detection grouping, shot snapping, the held queue cycle, and a real blur render on a tiny film."""
import os
os.environ.setdefault("KIZUNA_SUBTITLE_LANGUAGE", "ar")
import json
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch

import family
import library_processing as lp
import stremio as s
from test_audit import Sandbox


def row(t, strength=0.3, **p):
    r = {"t": t}
    for g in family.KINDS:
        r[g], r[g + "_abs"] = p.get(g, 0.0), strength
    return r


class Detection(unittest.TestCase):
    # rules from The Town calibration (2026-11-27): a strong frame starts a scene, nearby weaker frames support it
    def test_a_scene_needs_a_strong_frame_and_support(self):
        rows = [row(10, kiss=.4), row(10.5, kiss=.9), row(11, kiss=.5),           # a kiss: strong frame + support
                row(30, kiss=.97),                                                 # one strong frame alone: a passing face
                row(50, kiss=.5), row(50.5, kiss=.55),                             # support but never strong
                row(70, revealing=.7), row(71, revealing=.4)]                      # revealing, same rule
        got = family.scenes(rows)
        self.assertEqual([(x["from"], x["to"], x["kinds"]) for x in got], [(10, 11, ["kiss"]), (70, 71, ["revealing"])])

    def test_one_clear_nudity_frame_is_enough_and_weak_matches_never_count(self):
        # The Town 13:01: the strip-club shot was a single frame; the end credits won the softmax with nothing matching
        rows = [row(781, nudity=.99), row(7195, nudity=.9, strength=.15), row(7196, nudity=.8, strength=.15)]
        self.assertEqual([(x["from"], x["kinds"]) for x in family.scenes(rows)], [(781, ["nudity"])])

    def test_kinds_at_the_same_moment_are_one_scene(self):
        rows = [row(908, kiss=.9), row(909, kiss=.8, revealing=.7), row(910, revealing=.5)]
        got = family.scenes(rows)
        self.assertEqual([(x["from"], x["to"], x["kinds"]) for x in got], [(908, 910, ["kiss", "revealing"])])

    def test_a_scene_is_widened_to_its_whole_shot(self):
        # The Town 49:4x: the kiss shot starts at the 49:42.44 cut although the first flagged frame is 49:46
        with patch.object(family, "cuts", return_value=[2961.9, 2977.0, 2982.438, 2992.865, 3000.9]):
            self.assertEqual(family.shot_range("v", 2986.0, 2992.0), (2982.188, 2993.115))
        with patch.object(family, "cuts", return_value=[]):                    # no cut found: padded, never trimmed
            self.assertEqual(family.shot_range("v", 100.0, 104.0), (97.75, 106.25))

    def test_overlapping_keyframe_spans_merge(self):
        with patch.object(family, "clean_keys", return_value=[0, 10, 20, 30, 40, 50]):
            spans = family.plan("v", [(12, 15), (18, 19), (42, 44)])
        self.assertEqual([(x["k1"], x["k2"], x["blur"]) for x in spans],
                         [(10, 20, [[12, 15], [18, 19]]), (40, 50, [[42, 44]])])


class HeldCycle(Sandbox):
    """A download with the family audit is never published before the user approves the result."""
    def setUp(self):
        super().setUp()
        self.root = patch.object(lp, "ROOT", Path(self.tmp, "processing"))
        self.root.start()
        self.dest = Path(self.lib, "Film.mkv")
        self.key = lp.key_for(self.dest)
        self.folder = lp.ROOT / self.key
        self.folder.mkdir(parents=True)
        (self.folder / "media.mkv").write_bytes(b"prepared film")
        self.record = {"source": str(self.folder / "x.part"), "destination": str(self.dest), "manual": False, "status": "running",
                       "stage": "", "metadata": {"type": "movie", "family": True}, "backup": "b", "input_signature": [1, 1]}
        lp.save_record(self.key, dict(self.record))

    def tearDown(self):
        self.root.stop()
        super().tearDown()

    def fake_worker(self, *a, **k):
        args = a[0]
        phase = args[3]
        if phase == "scan":
            (self.folder / "review.json").write_text(json.dumps({"scenes": [
                {"id": 0, "from": 10, "to": 14, "kinds": ["kiss"], "peak": .9, "decision": None, "image": "scene0.jpg"},
                {"id": 1, "from": 50, "to": 52, "kinds": ["revealing"], "peak": .7, "decision": None, "image": "scene1.jpg"}],
                "added": []}), encoding="utf-8")
        else:
            (self.folder / "family-work").mkdir(exist_ok=True)
            (self.folder / "family-work" / "family.mkv").write_bytes(b"edited film")
            (self.folder / "proof.jpg").write_bytes(b"jpg")
        (self.folder / "family-result.json").write_text('{"ok": true}', encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stderr="")

    def test_review_apply_proof_approve_release(self):
        published = []
        with patch.object(lp.subprocess, "run", side_effect=self.fake_worker), \
                patch.object(lp, "publish", side_effect=lambda k, r, f: published.append((f / "media.mkv").read_bytes()) or r.update(status="done")):
            record = dict(lp.read()[self.key])
            lp.save_record(self.key, record)
            # the subtitle worker finished: instead of publishing, the title is held and scanned
            (self.folder / "result.json").write_text('{"ready": true, "stage": "ok"}', encoding="utf-8")
            record.update(status="family", family={"phase": "scan", "result": {"ready": True, "stage": "ok"}, "source": "media.mkv"})
            lp.family_step(self.key, record)
            self.assertEqual(lp.read()[self.key]["family"]["phase"], "review")
            self.assertEqual(published, [])
            with patch.object(lp, "process_record") as proc:                    # held for the user: the queue leaves it
                lp.poll({})
            proc.assert_not_called()
            with self.assertRaises(s.UserError):
                lp.family_apply(self.key)                                         # scenes still undecided
            lp.family_decide(self.key, 0, "blur")
            lp.family_decide(self.key, 1, "keep")
            with self.assertRaises(s.UserError):
                lp.family_add(self.key, 30, 20)                                   # end before start
            lp.family_add(self.key, 70, 75)
            self.assertEqual(lp.family_apply(self.key)["phase"], "render")
            lp.family_step(self.key, lp.read()[self.key])
            self.assertEqual(lp.family_review(self.key)["phase"], "proof")
            self.assertEqual(published, [])                                       # still nothing in the library
            lp.family_back(self.key)                                              # rejected: edit discarded
            self.assertFalse((self.folder / "family-work").exists())
            lp.family_apply(self.key)
            lp.family_step(self.key, lp.read()[self.key])
            lp.family_approve(self.key)
            lp.family_step(self.key, lp.read()[self.key])
        self.assertEqual(published, [b"edited film"])                             # the approved edit, nothing else
        self.assertEqual(lp.read()[self.key]["status"], "done")
        self.assertTrue(lp.read()[self.key]["family_released"])

    def test_nothing_to_blur_releases_the_film_unchanged(self):
        published = []
        with patch.object(lp.subprocess, "run", side_effect=self.fake_worker), \
                patch.object(lp, "publish", side_effect=lambda k, r, f: published.append((f / "media.mkv").read_bytes()) or r.update(status="done")):
            record = dict(lp.read()[self.key], status="family",
                          family={"phase": "scan", "result": {"ready": True, "stage": "ok"}, "source": "media.mkv"})
            lp.save_record(self.key, record)
            lp.family_step(self.key, record)
            lp.family_decide(self.key, 0, "keep")
            lp.family_decide(self.key, 1, "keep")
            self.assertEqual(lp.family_apply(self.key)["phase"], "release")
            lp.family_step(self.key, lp.read()[self.key])
        self.assertEqual(published, [b"prepared film"])


class RealRender(unittest.TestCase):
    """ffmpeg + mkvmerge on a 12 s synthetic film: only the keyframe span is re-encoded; everything else is identical."""
    def test_blur_is_inside_the_range_and_the_rest_is_bit_identical(self):
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="kizuna-family-"))
        src = d / "film.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=12",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:v", "libx264", "-g", "24", "-bf", "0",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True, capture_output=True)
        out = family.render(src, [(5.0, 6.0)], d / "work")
        self.assertEqual(family._count(out), family._count(src))
        def inside(x, y):                                                        # x appears unchanged, in order, in y
            return bool(x) and any(y[i:i + len(x)] == x for i in range(len(y) - len(x) + 1))
        self.assertTrue(inside(family._md5(src, 0, 3), family._md5(out, 0, 3.5)))        # before the edit: untouched
        self.assertTrue(inside(family._md5(src, 8, 3), family._md5(out, 7.5, 4)))        # after it: untouched
        self.assertFalse(set(family._md5(src, 5.2, .5)) & set(family._md5(out, 5, 1)))  # inside: every frame blurred
        audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(audio, "aac")                                             # the other tracks came along
        sheet = family.proof(out, [(5.0, 6.0)], d / "proof.jpg")
        self.assertGreater(os.path.getsize(sheet), 5000)


if __name__ == "__main__":
    unittest.main()
