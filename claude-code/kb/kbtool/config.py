"""Где лежит производная базы и что в её конфиге (SPEC §3, §8 S1/S6/S7).

Производная живёт в ``$KB_HOME/<slug>/`` (по умолчанию ``~/.kb``), вне папки документов.
``kb.yaml`` пишется как подмножество YAML: одна строка ``ключ: <JSON>`` — читается и этим
кодом, и любым YAML-парсером.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

DEFAULT_LIMITS = {
    "max_file_bytes": 50 * 1024 * 1024,      # S7: файл ≤ 50 МБ
    "max_files": 2000,                       # S7: корпус ≤ 2 000 файлов
    "max_text_bytes": 200 * 1024 * 1024,     # S7: ≤ 200 МБ текста
    "max_unit_chars": 12000,                 # единица ≤ 12 000 знаков → части
    "batch_chars": 60000,                    # пачка разметки ≤ 60 000 знаков
}
DEFAULTS = {
    "genres": ["reflection", "idea", "practical", "reference", "narrative", "mixed", "noise"],
    "categories": [],                         # пусто = категорию предлагает модель (MVP)
    "exclude": [],                            # шаблоны fnmatch относительно корня корпуса
    "include_forced": [],                     # явно включённые пути (только внутри корпуса)
    "vocab_corridor": [30, 70],
}
EDGE_TYPES = ("continues", "develops", "contradicts", "revisits", "applies", "duplicates")
EDGE_CONFIDENCE = ("extracted", "inferred", "ambiguous")
VERDICTS = ("ценно", "отвергнуто", "устарело", "спорно")
RESOLUTIONS = ("kb", "model", "both", "pending")

SYSTEM_ROOTS = ("/", "/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/var", "/private",
                "/Applications", "/opt", "/dev", "/Volumes", "/cores", "/tmp", "/proc", "/sys", "/boot",
                "/root", "/home", "/Users")


DEMO_SLUG = "kb-demo"                         # демо-база скилла /kb-start (образцы samples/)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class KbError(Exception):
    """Ошибка, которую пользователь должен увидеть словами (не трейсбек)."""


def kb_home() -> Path:
    return Path(os.environ.get("KB_HOME") or (Path.home() / ".kb")).expanduser()


def slug_for(corpus: Path) -> str:
    base = re.sub(r"[^\w.-]+", "-", corpus.name, flags=re.UNICODE).strip("-.")[:40] or "corpus"
    return f"{base}-{hashlib.sha256(str(corpus).encode()).hexdigest()[:6]}"


def check_corpus_root(path: str | os.PathLike) -> Path:
    """S1: корпус — существующая папка, не системная, не домашняя целиком, не содержит KB_HOME."""
    p = Path(path).expanduser()
    if not p.exists() or not p.is_dir():
        raise KbError(f"папки нет: {p}")
    real = Path(os.path.realpath(p))
    home = Path(os.path.realpath(Path.home()))
    if real == home:
        raise KbError("домашняя папка целиком не принимается — укажите конкретную папку документов")
    for root in SYSTEM_ROOTS:
        if str(real) == os.path.realpath(root) or str(real) == root:
            raise KbError(f"системная папка не принимается: {real}")
    if str(real).startswith("/System/") or str(real).startswith("/usr/") or str(real).startswith("/etc/"):
        raise KbError(f"системная папка не принимается: {real}")
    kh = Path(os.path.realpath(kb_home()))
    if kh == real or str(kh).startswith(str(real) + os.sep):
        raise KbError(f"папка базы {kh} лежит внутри корпуса — производная должна быть вне документов (S6)")
    if str(real).startswith(str(kh) + os.sep):
        raise KbError("корпус внутри папки баз ~/.kb не принимается")
    return real


def dump_yaml(data: dict) -> str:
    lines = ["# kb.yaml — конфиг корпуса (формат: «ключ: JSON», совместим с YAML)"]
    for k, v in data.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def load_yaml(text: str) -> dict:
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        key, sep, val = s.partition(":")
        if not sep:
            raise KbError(f"kb.yaml строка {n}: ожидается «ключ: значение»")
        try:
            out[key.strip()] = json.loads(val.strip())
        except json.JSONDecodeError as exc:
            raise KbError(f"kb.yaml строка {n}: значение не JSON ({exc.msg})") from None
    return out


class Base:
    """Одна база: ``$KB_HOME/<slug>/`` со всеми производными файлами."""

    def __init__(self, root: Path):
        self.root = root
        self.yaml = root / "kb.yaml"
        self.db = root / "kb.sqlite"
        self.markup = root / "markup"
        self.units_dir = root / "units"
        self.batches = root / "batches"
        self.runs = root / "runs"
        self.status = root / "status.json"
        self.scan_json = root / "scan.json"
        self.extract_json = root / "extract.json"

    @property
    def slug(self) -> str:
        return self.root.name

    def config(self) -> dict:
        if not self.yaml.exists():
            raise KbError(f"нет {self.yaml} — сначала kbtool init <папка>")
        cfg = {**DEFAULTS, **load_yaml(self.yaml.read_text(encoding="utf-8"))}
        cfg["limits"] = {**DEFAULT_LIMITS, **(cfg.get("limits") or {})}
        return cfg

    def save_config(self, cfg: dict) -> None:
        write_private(self.yaml, dump_yaml(cfg))

    def corpus(self) -> Path:
        return Path(self.config()["corpus_path"])

    def ensure_dirs(self) -> None:
        for d in (self.root, self.markup, self.units_dir, self.batches, self.runs):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)


def write_private(path: Path, text: str) -> None:
    """Атомарная запись текстового файла с правами 0600 (S6)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def init_base(corpus: str | os.PathLike, slug: str | None = None) -> Base:
    """Создать (или открыть) базу для папки. ``slug`` — явное имя базы (например, демо ``kb-demo``).

    Явное имя, уже занятое другой папкой, переносится на новую папку, только если прежней папки
    больше нет или это демо-база (после обновления плагина образцы лежат по новому пути). Разметка
    при этом остаётся годной: id единиц — хеш содержимого.
    """
    real = check_corpus_root(corpus)
    if slug is not None and not SLUG_RE.match(slug):
        raise KbError(f"имя базы {slug!r}: только строчные латинские буквы, цифры и дефис, до 40 знаков")
    home = kb_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    base = Base(home / (slug or slug_for(real)))
    base.ensure_dirs()
    if not base.yaml.exists():
        cfg = {"corpus_path": str(real), **DEFAULTS, "limits": dict(DEFAULT_LIMITS), "notice_shown": None}
        base.save_config(cfg)
    elif slug is not None:
        stored = load_yaml(base.yaml.read_text(encoding="utf-8"))
        old = stored.get("corpus_path")
        if old != str(real):
            if slug != DEMO_SLUG and old and os.path.isdir(old):
                raise KbError(f"имя базы «{slug}» уже занято папкой {old}")
            stored["corpus_path"] = str(real)
            base.save_config(stored)
    return base


def list_bases() -> list[Base]:
    home = kb_home()
    if not home.is_dir():
        return []
    return [Base(p) for p in sorted(home.iterdir()) if (p / "kb.yaml").is_file()]


def resolve_base(kb: str | None = None, corpus: str | None = None) -> Base:
    """База по slug, по пути корпуса или единственная существующая."""
    if corpus:
        real = Path(os.path.realpath(Path(corpus).expanduser()))
        base = Base(kb_home() / slug_for(real))
        if base.yaml.exists():
            return base
        for b in list_bases():                   # база с явным именем (init --slug)
            if b.config().get("corpus_path") == str(real):
                return b
        raise KbError(f"для {real} базы нет — kbtool init {real}")
    bases = list_bases()
    if kb:
        for b in bases:
            if b.slug == kb:
                return b
        raise KbError(f"базы «{kb}» нет; есть: {', '.join(b.slug for b in bases) or '—'}")
    # Демо-база из /kb-start не мешает выбору по умолчанию, если есть свои базы.
    bases = [b for b in bases if b.slug != DEMO_SLUG] or bases
    if len(bases) == 1:
        return bases[0]
    if not bases:
        raise KbError("баз нет — сначала /kb-build (kbtool init <папка>)")
    built = [b for b in bases if b.db.exists()]
    if len(built) == 1:
        return built[0]
    raise KbError(f"баз несколько, укажите --kb: {', '.join(b.slug for b in bases)}")
