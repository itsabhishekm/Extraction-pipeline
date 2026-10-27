"""Check that everything the pipeline needs is installed and reachable.

    python check_setup.py           # quick checks
    python check_setup.py --models  # also load each model once (slower, uses the GPU)

Each line prints OK, WARN (works, but worth fixing) or FAIL with what to do. Safe to run any time.
"""
import argparse
import importlib
import os
import shutil
import sys
from pathlib import Path

OLLAMA_MODELS = ["glm-ocr", "qwen3.5:4b"]
results = []


class Warn(Exception):
    """Worth fixing, but the pipeline runs anyway (counts as passed)."""


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"  OK    {name}" + (f"  ({detail})" if detail else ""))
    except Warn as w:
        results.append(True)
        print(f"  WARN  {name}: {w}")
    except Exception as e:  # noqa: BLE001 - report any failure and keep checking
        results.append(False)
        print(f"  FAIL  {name}: {e}")


def python_version():
    if sys.version_info < (3, 10):
        raise RuntimeError(f"Python {sys.version.split()[0]} found; 3.10 or newer needed")
    return sys.version.split()[0]


def packages():
    missing = []
    for mod in ["pymupdf", "pymupdf4llm", "psycopg", "yaml", "ollama", "qdrant_client", "FlagEmbedding", "torch", "jsonschema", "langsmith"]:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise RuntimeError(f"missing {missing} -> run setup.ps1 again")
    return "all installed"


def gpu():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(f"torch {torch.__version__} cannot see the GPU -> run setup.ps1 again (it reinstalls the CUDA build of PyTorch)")
    name = torch.cuda.get_device_name(0)
    free, total = torch.cuda.mem_get_info()
    return f"{name}, {free / 2**30:.1f} of {total / 2**30:.1f} GB free, torch {torch.__version__}"


def config_paths(cfg_path):
    import yaml
    raw = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    inp, data = Path(raw["input_root"]), Path(raw["data_root"])
    if not inp.is_dir():
        raise RuntimeError(f"input_root not found: {inp}")
    data.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(data).free / 2**30
    if free < 15:
        raise RuntimeError(f"only {free:.1f} GB free on the data drive ({data}); about 15 GB needed")
    return f"input {inp}, data {data} ({free:.0f} GB free)"


def user_env(var):
    """A variable from this process, or (Windows) the one saved for your user by setup.ps1,
    which a PowerShell window opened before setup does not see yet."""
    val = os.environ.get(var)
    if val or sys.platform != "win32":
        return val, False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, var)[0], True
    except OSError:
        return None, False


def env_on_data_drive():
    val, saved_only = user_env("OLLAMA_MODELS")
    if not val:
        default = Path.home() / ".ollama" / "models"
        raise Warn(f"OLLAMA_MODELS is not set, so Ollama keeps its models in {default} on C: (about 6 GB). "
                   "The pipeline works anyway. To move them: run setup.ps1 again")
    if saved_only:
        return f"OLLAMA_MODELS={val} (saved for your user; new PowerShell windows and Ollama after a restart use it)"
    return f"OLLAMA_MODELS={val}"


def postgres(cfg_path):
    import psycopg
    import yaml
    url = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))["database_url"]
    with psycopg.connect(url, connect_timeout=5) as conn:
        n_tables = conn.execute("SELECT count(*) FROM information_schema.tables "
                                "WHERE table_schema='cc' AND table_type='BASE TABLE'").fetchone()[0]
        n_fields = conn.execute("SELECT count(*) FROM cc.field_definitions").fetchone()[0]
        n_regs = conn.execute("SELECT count(*) FROM cc.regulations").fetchone()[0]
    if n_tables != 14 or n_fields != 31 or n_regs != 12:
        raise RuntimeError(f"{n_tables} tables / {n_fields} fields / {n_regs} regulations "
                           "(expected 14 / 31 / 12) -> recreate: docker compose down -v; docker compose up -d")
    return "14 tables, 31 field definitions, 12 regulations"


def qdrant():
    from qdrant_client import QdrantClient
    c = QdrantClient(url="http://localhost:6333", timeout=5)
    return f"{len(c.get_collections().collections)} collections"


def ollama_models():
    import ollama
    have = {m.model for m in ollama.list().models}
    missing = [m for m in OLLAMA_MODELS if not any(h == m or h.startswith(m + ":") or h == m + ":latest" for h in have)]
    if missing:
        raise RuntimeError(f"missing {missing} -> ollama pull " + " / ollama pull ".join(missing))
    return ", ".join(sorted(have))


def try_qwen():
    import ollama
    r = ollama.chat(model="qwen3.5:4b", messages=[{"role": "user", "content": "Reply with the word ready."}],
                    think=False, options={"num_ctx": 2048})
    return f"replied: {r.message.content.strip()[:40]!r}"


def try_glm_ocr():
    import base64
    import ollama
    import pymupdf
    page = pymupdf.open()
    p = page.new_page(width=400, height=120)
    p.insert_text((20, 60), "Late Fee: Up to $41", fontsize=20)
    png = p.get_pixmap(dpi=150).tobytes("png")
    r = ollama.chat(model="glm-ocr", messages=[{"role": "user", "content": "Text Recognition:",
                    "images": [base64.b64encode(png).decode()]}], options={"num_ctx": 16384})
    text = r.message.content.strip()
    if "41" not in text:
        raise RuntimeError(f"unexpected OCR output {text[:80]!r}")
    return f"read {text[:40]!r}"


BGE_M3_FILES = ["config.json", "pytorch_model.bin", "sparse_linear.pt", "colbert_linear.pt",
                "sentencepiece.bpe.model", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"]


def bge_m3_path(cfg_path) -> Path:
    import yaml
    raw = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    return Path(raw.get("models", {}).get("bge_m3_path", ""))


def bge_m3_files(cfg_path):
    folder = bge_m3_path(cfg_path)
    if not folder.is_dir():
        raise RuntimeError(f"folder not found: {folder} -> run setup.ps1 again (it downloads the BGE-M3 files)")
    missing = [f for f in BGE_M3_FILES if not (folder / f).is_file()]
    if missing:
        raise RuntimeError(f"missing in {folder}: {missing}")
    size = (folder / "pytorch_model.bin").stat().st_size / 2**30
    if size < 2.0:
        raise RuntimeError(f"pytorch_model.bin is only {size:.2f} GB (should be ~2.1 GB) - the download was cut short")
    return f"{folder}, all {len(BGE_M3_FILES)} files"


def try_bge_m3(cfg_path):
    from FlagEmbedding import BGEM3FlagModel
    m = BGEM3FlagModel(str(bge_m3_path(cfg_path)), use_fp16=True)
    out = m.encode(["The penalty APR may apply indefinitely."], return_dense=True, return_sparse=True)
    dense, sparse = out["dense_vecs"][0], out["lexical_weights"][0]
    return f"dense {len(dense)} dims, sparse {len(sparse)} terms"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--models", action="store_true", help="also run each model once")
    a = ap.parse_args()

    print("\nBasics")
    check("Python 3.10+", python_version)
    check("Python packages", packages)
    check("GPU visible to PyTorch", gpu)
    check("config paths and free space", lambda: config_paths(a.config))
    check("model folders on the data drive", env_on_data_drive)
    print("\nServices")
    check("Postgres (docker)", lambda: postgres(a.config))
    check("Qdrant (docker)", qdrant)
    check("Ollama models downloaded", ollama_models)
    check("BGE-M3 files in place", lambda: bge_m3_files(a.config))
    if a.models:
        print("\nModels (one small test each)")
        check("Qwen3.5-4B answers", try_qwen)
        check("GLM-OCR reads an image", try_glm_ocr)
        check("BGE-M3 dense + sparse", lambda: try_bge_m3(a.config))

    ok = sum(results)
    print(f"\n{ok}/{len(results)} checks passed" + ("" if ok == len(results) else " - fix the FAIL lines above"))
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
