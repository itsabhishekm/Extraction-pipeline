# Answer key (offline evals)

One JSON file per hand-checked card version. Create pre-filled files with

    python run.py eval --make-template --count 150

then, for each file: open the PDFs listed in "files", correct every field (status, raw_value,
normalized_value, quote, source_pdf, page), put your name in "labelled_by" and set "checked": true.
Only checked files are scored by `python run.py eval`.

Files with "used_as_example": true feed the prompt examples (`python run.py eval --make-examples`)
and are never scored - label those in addition to the 150.
