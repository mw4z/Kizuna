"""Family audit worker (run by library_processing.family_step): family_worker.py <folder> scan|render <source name>
scan:   candidate scenes + frame strips -> review.json
render: blur the scenes the user chose (+ the ones they added) -> family-work/family.mkv, verified, + proof.jpg
Writes family-result.json {"ok": bool, "error": str}. The source file is only read."""
import json
import sys
import traceback
from pathlib import Path

import agent
import family

if __name__ == "__main__":
    folder, phase, source = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    agent.LOG = str(folder / "family.log")
    result = {"ok": False}
    try:
        try:
            import psutil
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)     # the PC stays usable during a 4K scan
        except Exception:
            pass
        video = folder / source
        if phase == "scan":
            found = family.audit(folder, video)
            agent.log(f"family scan: {len(found)} scene(s) to review")
        elif phase == "render":
            review = json.loads((folder / "review.json").read_text(encoding="utf-8"))
            ranges = [(s["from"], s["to"]) for s in review["scenes"] if s["decision"] == "blur"]
            for extra in review["added"]:               # a missed scene: widened to its shot like a found one
                ranges.append(family.shot_range(video, extra["from"], extra["to"]))
            merged = []
            for a, b in sorted(ranges):
                if merged and a <= merged[-1][1] + 0.5:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            out = family.render(video, merged, folder / "family-work")
            family.proof(out, merged, folder / "proof.jpg")
            review["blurred"] = merged
            agent.write_atomic(folder / "review.json", json.dumps(review, ensure_ascii=False, indent=1))
            agent.log(f"family render: {len(merged)} range(s) blurred and verified")
        else:
            raise ValueError(phase)
        result = {"ok": True}
    except Exception as error:
        agent.log("family worker failed: " + traceback.format_exc()[-600:])
        result = {"ok": False, "error": f"{type(error).__name__}: {str(error)[:200]}"}
    (folder / "family-result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    sys.exit(0 if result["ok"] else 1)
