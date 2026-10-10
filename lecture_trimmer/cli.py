"""Create question-solving, theory, and combined videos from one lecture analysis."""
import argparse
import logging
from pathlib import Path

import lecture_trimmer.config as config
from lecture_trimmer.models import Label
from lecture_trimmer.processing.pipeline import OUTPUT_TITLES, process_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path)
    ap.add_argument("--outputs", nargs="+", choices=list(OUTPUT_TITLES), default=list(OUTPUT_TITLES))
    ap.add_argument("--out", type=Path, help="optional MP4 path for the combined video")
    ap.add_argument("--language", help="spoken language, e.g. Hindi or en")
    ap.add_argument("--context", default="", help="subject/chapter hint")
    ap.add_argument("--classifier", choices=["auto", "claude", "gemini"], default=config.CLASSIFIER_BACKEND)
    ap.add_argument("--mode", choices=list(config.MODES), default=config.DEFAULT_MODE)
    ap.add_argument("--cut-style", choices=["smooth", "fast"], default="smooth",
                    help="smooth: precise H.264 cuts and audio fades; fast: keyframe stream copy")
    ap.add_argument("--keep", help="override labels for combined video only, comma separated")
    ap.add_argument("--retranscribe", "--fresh", action="store_true", help="ignore analysis caches")
    ap.add_argument("--dry-run", action="store_true", help="write cut lists without exporting video")
    ap.add_argument("--visual-review", action="store_true",
                    help="send up to 24 selected frames to Gemini to clarify uncertain activities")
    args = ap.parse_args()
    keep = {s.strip() for s in args.keep.split(",") if s.strip()} if args.keep is not None else None
    if keep is not None and (not keep or keep - {l.value for l in Label}):
        ap.error("--keep must contain valid lecture labels")
    try:
        result = process_video(args.video, outputs=args.outputs, mode=args.mode, context=args.context,
                               language=args.language, classifier=args.classifier,
                               cut_style=args.cut_style, fresh=args.retranscribe, dry_run=args.dry_run,
                               combined_labels=keep, combined_out=args.out, visual_review=args.visual_review)
    except Exception as exc:
        logging.error("Processing failed: %s", exc)
        return 1
    for kind, item in result["outputs"].items():
        logging.info("%s: %s (%.1f min) %s", OUTPUT_TITLES[kind], item["status"],
                     item["duration"] / 60, item["path"] or "")
    logging.info("Report: %s", result["report_path"])
    return 1 if result.get("status") in {"failed", "partial"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
