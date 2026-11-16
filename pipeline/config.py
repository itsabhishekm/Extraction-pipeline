"""Load config.yaml and work out the folders the pipeline writes to."""
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict
    quarter: str
    input_root: Path
    data_root: Path
    database_url: str

    @property
    def text_dir(self) -> Path:
        return self.data_root / "text"

    @property
    def log_dir(self) -> Path:
        return self.data_root / "logs"

    def section(self, name: str) -> dict:
        return self.raw.get(name, {})


def _norm_folder(name: str) -> str:
    """Folder names compared without case, extra spaces or trailing dots (Windows drops trailing
    dots from folder names, so "CITIBANK, N.A." in a list is the folder "CITIBANK, N.A")."""
    return " ".join(name.split()).rstrip(". ").casefold()


def issuer_list(cfg: "Config") -> dict | None:
    """The issuers to process, from config `issuers_file` (a CSV with a folder_name column, optional
    entity column). Returns {normalized folder name: (folder name as listed, entity)}, or None when no
    list is set (= every issuer folder)."""
    import csv
    f = cfg.raw.get("issuers_file")
    if not f:
        return None
    path = Path(f) if Path(f).is_absolute() else HERE / f
    if not path.exists():
        raise SystemExit(f"issuers_file not found: {path}  (edit config.yaml)")
    out = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("folder_name") or "").strip()
            if name and name.upper() != "TOTAL":
                out[_norm_folder(name)] = (name, (row.get("entity") or "").strip())
    return out


def issuer_selected(cfg: "Config", folder_name: str, wanted: dict | None = None) -> bool:
    wanted = issuer_list(cfg) if wanted is None else wanted
    return wanted is None or _norm_folder(folder_name) in wanted


def load_config(path: str | None = None) -> Config:
    path = Path(path or os.environ.get("CC_CONFIG", HERE / "config.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = Config(
        raw=raw,
        quarter=str(raw["quarter"]),
        input_root=Path(raw["input_root"]),
        data_root=Path(raw["data_root"]),
        database_url=os.environ.get("CC_DATABASE_URL", raw["database_url"]),
    )
    if not cfg.input_root.is_dir():
        raise SystemExit(f"input_root not found: {cfg.input_root}  (edit config.yaml)")
    cfg.text_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# ----------------------------------------------------------------------------- logging
# Every line says which stage wrote it and where the stage is in its job:
#
#   2026-10-26 22:16:01  EXTRACT   INFO   [ 7/20  35%  eta 42m | v12] pricing: 38.0s, pages D1p1,D1p2
#
# Console lines are colour-coded: one colour per stage, warnings yellow, errors red, progress dim.
# Log files (no colours):
#   <data_root>/logs/<date>_<stage>.log    one per stage per day
#   <data_root>/logs/<date>_pipeline.log   every stage in one timeline
# Colours: config `logging: {color: auto | always | never}`; the NO_COLOR environment variable turns them off.

STAGE_COLORS = {          # ANSI colour codes
    "scan": "36", "text": "34", "ocr": "35", "group": "96", "extract": "32", "validate": "33",
    "retry": "91", "review": "95", "embed": "94", "report": "37", "integrity": "92", "eval": "36;1",
    "label": "35;1", "publish": "32;1",
}
LEVEL_COLORS = {"WARNING": "33;1", "ERROR": "31;1", "CRITICAL": "97;41"}


class Progress:
    """Where the current stage is in its job. Stages call:
        progress.start(total, "versions")      before the loop
        progress.step(i, "v12")               at each item (i starts at 1)
        progress.grow(k)                      when k more items are added (multi-card split)
        progress.stop()                       after the loop
    """

    def __init__(self):
        self.stop()

    def start(self, total: int, unit: str = "items") -> None:
        import time
        self.total, self.unit, self.i, self.item, self.t0 = int(total), unit, 0, "", time.time()

    def step(self, i: int, item: str = "") -> None:
        self.i, self.item = int(i), str(item)

    def grow(self, k: int) -> None:
        self.total += int(k)

    def stop(self) -> None:
        self.total, self.unit, self.i, self.item, self.t0 = 0, "", 0, "", None

    def text(self) -> str:
        if not self.total or not self.i:
            return ""
        import time
        done = self.i                            # lines are mostly written as an item finishes
        pct = 100 * done / self.total
        eta = ""
        if self.t0 and done < self.total:
            left = (time.time() - self.t0) / done * (self.total - done)
            h, m = int(left // 3600), int(left % 3600 // 60)
            eta = f"  eta {h}h{m:02d}m" if h else (f"  eta {m}m" if m else "  eta <1m")
        w = len(str(self.total))
        item = f" | {self.item}" if self.item else ""
        return f"[{self.i:>{w}}/{self.total} {pct:3.0f}%{eta}{item}] "


progress = Progress()


def _enable_windows_ansi() -> bool:
    """Turn on ANSI colour support in the Windows console (Windows 10+)."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)                  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # noqa: BLE001 - no console: no colours
        return False


class StageFormatter(logging.Formatter):
    def __init__(self, stage: str, color: bool):
        super().__init__()
        self.stage, self.color = stage, color

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", self.stage)
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        tag, lvl, prog = f"{stage.upper():<9}", f"{record.levelname:<7}", progress.text()
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        if not self.color:
            return f"{ts}  {tag} {lvl} {prog}{msg}"
        c = STAGE_COLORS.get(stage, "37")
        lc = LEVEL_COLORS.get(record.levelname)
        tag = f"\033[{c};7m {stage.upper():<8}\033[0m"          # stage name as a coloured block
        lvl = f"\033[{lc}m{record.levelname:<7}\033[0m" if lc else f"\033[2m{record.levelname:<7}\033[0m"
        prog = f"\033[{c}m{prog}\033[0m" if prog else ""
        body = f"\033[{lc}m{msg}\033[0m" if lc else msg
        return f"\033[2m{ts}\033[0m {tag} {lvl} {prog}{body}"


class _StageTag(logging.Filter):
    def __init__(self, stage: str):
        super().__init__()
        self.stage = stage

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "stage"):
            record.stage = self.stage
        return True


def setup_logging(cfg: Config, stage: str) -> logging.Logger:
    """Log to the console (coloured), to <date>_<stage>.log and to <date>_pipeline.log."""
    want = str(cfg.section("logging").get("color", "auto")).lower()
    color = want == "always" or (want == "auto" and not os.environ.get("NO_COLOR")
                                 and hasattr(sys.stdout, "isatty") and sys.stdout.isatty())
    if color:
        color = _enable_windows_ansi() or want == "always"
    day = f"{datetime.now():%Y%m%d}"
    console = logging.StreamHandler()
    console.setFormatter(StageFormatter(stage, color))
    handlers = [console]
    for name in (f"{day}_{stage}.log", f"{day}_pipeline.log"):
        h = logging.FileHandler(cfg.log_dir / name, encoding="utf-8")
        h.setFormatter(StageFormatter(stage, False))
        handlers.append(h)
    for h in handlers:
        h.addFilter(_StageTag(stage))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "urllib3", "FlagEmbedding", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)   # hide one line per HTTP call to Ollama
    progress.stop()
    return logging.getLogger(stage)
