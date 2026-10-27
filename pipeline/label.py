"""Label the answer key: go through the pre-filled answer-key files field by field.

    python run.py label                      # every file not yet checked, one after another
    python run.py label --file <name>.json   # one file
    python run.py label --reviewer Abhi

For each card it lists the PDFs (and opens them on request), then shows each field with the
pipeline's answer and its quote. Per field:
    Enter  keep the answer as it is (it is correct)
    e      edit: type the right status / value / normalized value / quote / PDF / page
    n      not_present (the PDFs do not mention it)
    o      open the PDFs again
    b      back to the previous field
    q      save progress and stop (continue later where you left off)
Every edit is checked: the status must be valid, the normalized value must match its format in
fields.yaml, and a found / stated_none value needs a quote, a PDF of this card and a page.
When every field is done the file gets "checked": true and your name in "labelled_by".
"""
import getpass
import json
import os
import subprocess
import sys

import jsonschema

from .config import Config
from .evals import key_dir
from .postprocess import STATUSES
from .stage_extract import fields_by_name, load_fields

FOUND = ("found", "stated_none")


def pdf_paths(cfg, key) -> dict:
    """file name -> path on disk, for the PDFs of this answer-key card."""
    folder = cfg.input_root / key["issuer_folder"]
    return {f: folder / f for f in key["files"]}


def open_pdfs(paths):
    for p in paths.values():
        if not p.exists():
            print(f"   not found: {p}")
            continue
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # noqa: S606 - opens the PDF in the default viewer
            else:
                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(p)])
        except OSError as e:
            print(f"   could not open {p.name}: {e}")


def show(i, n, name, fdef, f):
    print(f"\n[{i}/{n}] {name}: {fdef['question']}")
    for r in fdef.get("rules") or []:
        print(f"        rule: {r}")
    print(f"   status:     {f.get('status')}")
    print(f"   value:      {f.get('raw_value')}")
    print(f"   normalized: {json.dumps(f.get('normalized_value'))}")
    if f.get("quote"):
        print(f"   quote:      {f['quote'][:400]}")
        print(f"   source:     {f.get('source_pdf')} page {f.get('page')}")


def ask(prompt, default=None):
    v = input(f"   {prompt}" + (f" [{default}]" if default not in (None, "") else "") + " > ").strip()
    return v if v else default


def edit(name, fdef, f, files) -> dict:
    """Ask for a corrected answer until it passes the checks."""
    while True:
        status = ask(f"status {STATUSES}", f.get("status"))
        if status not in STATUSES:
            print("   ! status must be one of", STATUSES)
            continue
        new = {"status": status, "raw_value": None, "normalized_value": None, "quote": None,
               "source_pdf": None, "page": None}
        if status in FOUND:
            new["raw_value"] = ask("value exactly as written", f.get("raw_value"))
            norm = fdef["normalized"]
            if norm.get("type") != "null":
                shape = json.dumps(norm.get("properties") and {k: v.get("type") for k, v in norm["properties"].items()}
                                   or norm)
                raw_norm = ask(f"normalized value as JSON, shape {shape}", json.dumps(f.get("normalized_value")))
                try:
                    new["normalized_value"] = json.loads(raw_norm) if raw_norm not in (None, "", "null") else None
                    if new["normalized_value"] is not None:
                        jsonschema.validate(new["normalized_value"], norm)
                except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                    print(f"   ! normalized value not valid: {getattr(e, 'message', e)}")
                    continue
            new["quote"] = ask("quote, copied from the PDF", f.get("quote"))
            for k, fn in enumerate(files, 1):
                print(f"      {k}. {fn}")
            src = ask("source PDF (number or name)", f.get("source_pdf"))
            if src and src.isdigit() and 1 <= int(src) <= len(files):
                src = files[int(src) - 1]
            if src not in files:
                print("   ! the source PDF must be one of this card's files")
                continue
            new["source_pdf"] = src
            page = ask("page number", f.get("page"))
            if not str(page or "").isdigit():
                print("   ! page must be a number")
                continue
            new["page"] = int(page)
            if not new["quote"]:
                print("   ! a found / stated_none value needs a quote")
                continue
        return new


def label_file(cfg, path, reviewer, fdefs) -> str:
    """Returns 'done', 'stopped' or 'skipped'."""
    key = json.loads(path.read_text(encoding="utf-8"))
    paths = pdf_paths(cfg, key)
    names = list(fdefs)
    done = set(key.get("labelled_fields", []))
    print("\n" + "=" * 78)
    print(f"{path.name}\nissuer: {key['issuer_folder']}" + (f"\nproduct: {key['product_name']}" if key.get("product_name") else ""))
    for f in key["files"]:
        print(f"   file: {f}")
    print(f"{len(done)} of {len(names)} fields already labelled")
    a = input("[Enter] start  [o] open the PDFs and start  [s] skip this card  [q] quit > ").strip().lower()
    if a == "q":
        return "stopped"
    if a == "s":
        return "skipped"
    if a == "o":
        open_pdfs(paths)
    i = 0
    while i < len(names):
        name = names[i]
        if name in done and a != "b":
            i += 1
            continue
        f = key["fields"].setdefault(name, {"status": "not_present"})
        show(i + 1, len(names), name, fdefs[name], f)
        a = input("   [Enter] correct  [e] edit  [n] not_present  [o] open PDFs  [b] back  [q] save+stop > ").strip().lower()
        if a == "q":
            key["labelled_fields"] = sorted(done)
            path.write_text(json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"   saved ({len(done)} of {len(names)} fields)")
            return "stopped"
        if a == "o":
            open_pdfs(paths)
            continue
        if a == "b":
            i = max(0, i - 1)
            done.discard(names[i])
            continue
        if a == "e":
            key["fields"][name] = edit(name, fdefs[name], f, key["files"])
        elif a == "n":
            key["fields"][name] = {"status": "not_present", "raw_value": None, "normalized_value": None,
                                   "quote": None, "source_pdf": None, "page": None}
        elif f.get("status") in FOUND and (not f.get("quote") or f.get("source_pdf") not in key["files"]):
            print("   ! this answer has no quote or no valid source PDF; edit it (e) or mark it not_present (n)")
            continue
        done.add(name)
        key["labelled_fields"] = sorted(done)
        path.write_text(json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8")
        i += 1
    key.update(checked=True, labelled_by=reviewer)
    key.pop("labelled_fields", None)
    path.write_text(json.dumps(key, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   {path.name}: checked by {reviewer}")
    return "done"


def run(cfg: Config, file=None, reviewer=None) -> dict:
    reviewer = reviewer or getpass.getuser()
    fdefs = fields_by_name(load_fields())
    d = key_dir(cfg)
    files = [d / file] if file else sorted(p for p in d.glob("*.json")
                                           if not json.loads(p.read_text(encoding="utf-8")).get("checked"))
    counts = {"done": 0, "skipped": 0, "stopped": 0}
    total_checked = sum(1 for p in d.glob("*.json") if json.loads(p.read_text(encoding="utf-8")).get("checked"))
    print(f"{len(files)} answer-key files to label in {d} ({total_checked} already checked)")
    for p in files:
        if not p.exists():
            print(f"not found: {p}")
            continue
        r = label_file(cfg, p, reviewer, fdefs)
        counts[r] += 1
        if r == "stopped":
            break
    print(counts)
    return counts
