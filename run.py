"""Pipeline entry point. Run the stages in this order (each only picks up work not done yet):

    python run.py scan        # 1. find and hash every PDF
    python run.py text        # 2. classify pages, write .md files       (--retry: retry failed PDFs)
    python run.py ocr         # 3. GLM-OCR reads the scanned pages
    python run.py group       # 4. decide which PDFs form each card version
    python run.py extract     # 5. Qwen fills the fields -> JSON + database (--version N, --redo)
    python run.py validate    # 6. Validation Gate; failures go to the retry queue
    python run.py retry       # 7. Repair Agent: re-ask only the failed values
    python run.py review      # 8. Human Review of what the Repair Agent could not fix
                              #    (--export file.csv / --import file.csv for Excel)
    python run.py embed       # 9. BGE-M3 dense + sparse vectors -> Qdrant (--redo)
    python run.py report      #    bank inventory files + quarter summary (audit-v6 outputs)
    python run.py integrity   #    Integrity Check: counts match + test searches pass
    python run.py eval        #    offline evals + release gate (--make-template, --make-examples)
    python run.py label       #    hand-check the answer-key files field by field (--file, --reviewer)
    python run.py publish     #    report + integrity + eval gate, then copy a release
    python run.py search "can I opt out of arbitration" [--issuer NAME] [--top 5]
    python run.py status      # where every PDF and card version is

    python run.py all         # scan .. retry (retry repeated up to max_retries), embed, report, status

Add --config config.pilot.yaml to work on the pilot files only.
"""
import argparse
import sys

from pipeline import (embed, evals, integrity, label, publish, report, review, stage_extract, stage_group, stage_ocr,
                      stage_scan, stage_text, stage_validate, status)
from pipeline.config import load_config

ORDER = ["scan", "text", "ocr", "group", "extract", "validate", "retry"]
OTHER = ["label", "review", "embed", "report", "integrity", "eval", "publish", "search", "status", "all"]


def main() -> int:
    p = argparse.ArgumentParser(description="CFPB credit card agreement pipeline")
    p.add_argument("command", choices=ORDER + OTHER)
    p.add_argument("query", nargs="?", help="search: the question")
    p.add_argument("--config", help="path to a config.yaml (default: the one next to run.py)")
    p.add_argument("--retry", action="store_true", help="text stage: retry documents that failed")
    p.add_argument("--version", type=int, action="append", help="extract: only this version id (repeatable)")
    p.add_argument("--redo", action="store_true", help="extract / validate / embed: redo versions already done")
    p.add_argument("--export", help="review: write open items to this CSV")
    p.add_argument("--import", dest="import_", help="review: apply decisions from this CSV")
    p.add_argument("--reviewer", help="review / label: your name (default: your login)")
    p.add_argument("--file", help="label: only this answer-key file")
    p.add_argument("--make-template", action="store_true", help="eval: write answer-key templates")
    p.add_argument("--count", type=int, default=150, help="eval --make-template: how many cards")
    p.add_argument("--make-examples", action="store_true", help="eval: build examples.yaml from the answer key")
    p.add_argument("--skip-eval", action="store_true", help="publish: publish without the eval gate (not approved)")
    p.add_argument("--issuer", help="search: only this issuer folder")
    p.add_argument("--card", help="search: only this card name")
    p.add_argument("--top", type=int, default=5, help="search: number of results")
    args = p.parse_args()
    cfg = load_config(args.config)
    cmd = args.command

    if cmd == "review":
        review.run(cfg, export=args.export, import_=args.import_, reviewer=args.reviewer)
        return 0
    if cmd == "label":
        label.run(cfg, file=args.file, reviewer=args.reviewer)
        return 0
    if cmd == "search":
        if not args.query:
            p.error('search needs a question, e.g. python run.py search "late fee"')
        embed.print_search(cfg, args.query, args.top, args.issuer, args.card)
        return 0
    if cmd == "integrity":
        return 0 if integrity.run(cfg) else 1
    if cmd == "eval":
        return evals.run(cfg, args.make_template, args.count, args.make_examples)
    if cmd == "publish":
        return 0 if publish.run(cfg, skip_eval=args.skip_eval) else 1
    if cmd == "report":
        report.run(cfg)
        return 0
    if cmd == "embed":
        embed.run(cfg, redo=args.redo)
        return 0

    todo = ORDER if cmd == "all" else [cmd] if cmd != "status" else []
    for step in todo:
        if step == "scan":
            stage_scan.run(cfg)
        elif step == "text":
            stage_text.run(cfg, retry_failed=args.retry)
        elif step == "ocr":
            stage_ocr.run(cfg)
        elif step == "group":
            stage_group.run(cfg)
        elif step == "extract":
            stage_extract.run(cfg, version_ids=args.version, redo=args.redo)
        elif step == "validate":
            stage_validate.run(cfg, redo=args.redo)
        elif step == "retry":
            rounds = cfg.section("validate").get("max_retries", 2) + 1 if cmd == "all" else 1
            for _ in range(rounds):
                c = stage_validate.run_retry(cfg)
                if not c["still_queued"]:
                    break
    if cmd == "all":
        embed.run(cfg)
        report.run(cfg)
    if cmd in ("status", "all"):
        status.run(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
