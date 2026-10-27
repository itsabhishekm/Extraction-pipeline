"""Calls to the local Ollama server: OCR (GLM-OCR) and JSON extraction (Qwen3.5-4B).

Every call is traced: one JSON line per call in <data_root>/traces/<date>.jsonl with the
stage, model, purpose, version, full prompt and output, token counts and seconds (local
models cost no money, so "cost" is GPU time and tokens). If config `tracing.langsmith` is
true and LANGSMITH_API_KEY is set, calls are also sent to LangSmith (this uploads the
prompts and outputs to LangSmith's cloud).

Only one model fits in 6 GB, so each stage calls unload() for its model when done.
Set CC_FAKE_LLM=1 to use tests/fake_llm.py instead of Ollama (testing without a GPU).
"""
import base64
import json
import os
import time
from datetime import datetime, timezone

_client = None
_trace = {"dir": None, "stage": None, "full_text": True, "langsmith": False, "run_id": None}
# Output caps. num_predict counts thinking + answer tokens. Without a cap, Qwen can think (or
# repeat itself) until the 16k context is full: ~5 minutes and no answer. Set in config `llm:`.
_limits = {"max_tokens": 4096, "think_max_tokens": 7000, "num_ctx": 16384}


def configure(cfg, stage: str, run_id: int | None = None) -> None:
    _limits.update({k: v for k, v in cfg.section("llm").items() if k in _limits})
    t = cfg.section("tracing")
    _trace.update(dir=cfg.data_root / "traces", stage=stage, full_text=t.get("full_text", True), run_id=run_id,
                  langsmith=bool(t.get("langsmith", False) and os.environ.get("LANGSMITH_API_KEY")))
    _trace["dir"].mkdir(parents=True, exist_ok=True)
    if _trace["langsmith"]:
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", t.get("langsmith_project", f"cc-pipeline-{cfg.quarter}"))


def _ollama():
    global _client
    if _client is None:
        if os.environ.get("CC_FAKE_LLM") == "1":
            from tests import fake_llm
            _client = fake_llm
        else:
            import ollama
            _client = ollama
    return _client


def _write_trace(rec: dict) -> None:
    if not _trace["dir"]:
        return
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stage": _trace["stage"],
           "run_id": _trace["run_id"], **rec}
    path = _trace["dir"] / f"{datetime.now():%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _chat(kind: str, model: str, messages: list, meta: dict, **kw):
    """One traced chat call."""
    def call():
        return _ollama().chat(model=model, messages=messages, **kw)

    t0 = time.time()
    if _trace["langsmith"]:
        try:
            from langsmith import traceable
            r = traceable(run_type="llm", name=f"{_trace['stage']}:{kind}",
                          metadata={**meta, "model": model, "run_id": _trace["run_id"]})(lambda **_: call())(
                messages=[{k: v for k, v in m.items() if k != "images"} for m in messages])
        except ImportError:
            r = call()
    else:
        r = call()
    secs = round(time.time() - t0, 2)
    rec = {"kind": kind, "model": model, **meta, "seconds": secs,
           "prompt_tokens": getattr(r, "prompt_eval_count", None), "output_tokens": getattr(r, "eval_count", None),
           "images": sum(len(m.get("images") or []) for m in messages),
           "think": kw.get("think"), "done_reason": getattr(r, "done_reason", None)}
    if _trace["full_text"]:
        rec["input"] = [{k: v for k, v in m.items() if k != "images"} for m in messages]
        rec["output"] = r.message.content
    _write_trace(rec)
    return r, secs


def ocr_page(model: str, png: bytes, num_ctx: int = 16384, retries: int = 2, meta: dict | None = None) -> str:
    """Return the text GLM-OCR reads from one page image (markdown, tables as markdown/HTML)."""
    last = None
    for attempt in range(retries + 1):
        try:
            r, _ = _chat("ocr", model, [{"role": "user", "content": "Text Recognition:",
                                         "images": [base64.b64encode(png).decode()]}],
                         meta or {}, options={"num_ctx": num_ctx, "temperature": 0})
            return r.message.content.replace("\x00", "").strip()
        except Exception as e:  # noqa: BLE001 - retry, then raise the last error
            last = e
            _write_trace({"kind": "ocr", "model": model, **(meta or {}), "error": str(e)})
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OCR failed after {retries + 1} tries: {last}")


class Truncated(ValueError):
    """The model hit the output cap before finishing its answer."""


def extract_json(model: str, system: str, user: str, schema: dict, think: bool, num_ctx: int | None = None,
                 retries: int = 1, images: list[bytes] | None = None, kind: str = "extract",
                 meta: dict | None = None) -> tuple[dict, dict]:
    """Ask the model for JSON matching `schema`. Optional page images (vision). Returns (JSON, stats).

    Attempts, each capped in output length:
      1. as asked (thinking on or off), temperature 0
      2. if 1 ran out of tokens / gave no JSON: thinking OFF and a repeat penalty
         (an identical retry would loop the same way)
      3. once more like 2 (`retries` extra tries in total after the first)
    """
    num_ctx = num_ctx or _limits["num_ctx"]
    msg = {"role": "user", "content": user}
    if images:
        msg["images"] = [base64.b64encode(i).decode() for i in images]
    plan = [(think, {"temperature": 0})]
    # retries stay at temperature 0: sampling (temperature > 0) made the grammar write invented digits
    # (e.g. a $40 fee read as 39.5625); the repeat penalty is what breaks a loop
    plan += [(False, {"temperature": 0, "repeat_penalty": 1.2})] * (retries + (1 if think else 0))
    last = None
    for n, (th, opts) in enumerate(plan):
        cap = _limits["think_max_tokens"] if th else _limits["max_tokens"]
        try:
            r, secs = _chat(kind, model, [{"role": "system", "content": system}, msg],
                            {**(meta or {}), "attempt": n + 1},
                            format=schema, think=th, options={"num_ctx": num_ctx, "num_predict": cap, **opts})
            content = r.message.content or ""
            if getattr(r, "done_reason", None) == "length" or not content.strip():
                raise Truncated(f"no complete answer within {cap} tokens (thinking {'on' if th else 'off'})")
            data = json.loads(content)
            return data, {"seconds": secs, "prompt_tokens": getattr(r, "prompt_eval_count", None),
                          "output_tokens": getattr(r, "eval_count", None), "attempt": n + 1, "think": th}
        except Exception as e:  # noqa: BLE001 - cut off, bad JSON or a server hiccup: next attempt
            last = e
            _write_trace({"kind": kind, "model": model, **(meta or {}), "attempt": n + 1,
                          "error": f"{type(e).__name__}: {e}"})
            if not isinstance(e, (Truncated, json.JSONDecodeError)):
                time.sleep(2)
    raise RuntimeError(f"{kind} call failed after {len(plan)} tries: {last}")


def unload(model: str) -> None:
    """Free the GPU memory held by `model` (keep_alive=0)."""
    try:
        _ollama().generate(model=model, prompt="", keep_alive=0)
    except Exception:  # noqa: BLE001 - not loaded / server gone: nothing to free
        pass
