"""Pipeline configuration (BUILD §8).

`<vp_home>/config.yaml` is created with DEFAULTS on first load. User values are deep-merged over
DEFAULTS (new default keys appear automatically). Per job: the profile named in request.yaml is
deep-merged with request.yaml `overrides` (e.g. the 3-minute test fixture shortens `long`).

The config holds only paths and env var NAMES — never secret values.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from .errors import VPError
from .io import read_yaml, write_yaml
from .paths import vp_home

# Single source of every config key the code reads, with its default. Modules keep inline
# fallbacks for robustness; tests/unit/test_config_defaults.py asserts they equal these values
# and that every key read in src/vp exists here.

_PROFILE_BASE: dict[str, Any] = {
    "reel": {"min_s": 30, "max_s": 90},
    "long": {"min_s": 600, "max_s": 2400},
    # S3a/S3b (vp.steps._fragments, s3a_select, s3b_review)
    "selection_margin": 1.5,
    "selection_expected_per_hour": {"reel": 8.0, "long": 2.0},
    "selection_full_scan": True,
    # 9:16 centre crop of a 21:9 frame: w = H*9/16 / W = 1440*0.5625/3440   (S4)
    "reel_crop": {"x_frac": 0.3823, "w_frac": 0.2355},
    # S1 (vp.steps.s1_proofread.s1_settings); request.yaml `s1:` overrides per job
    "s1": {"chunk_size": 350, "empty_share_max": 0.15, "echo_patterns": [],
           "after_speech_filter": True, "silence_db": -40.0, "silence_min_s": 0.5},
    "names": [],
}

DEFAULTS: dict[str, Any] = {
    "judge": "claude_cli",          # claude_cli | fake | manual   (env VP_JUDGE overrides)
    "judge_model": "sonnet",
    "judge_timeout_s": 900,
    "publisher": "youtube",         # youtube | fake (rehearsal: nothing leaves the machine);
                                    # request.yaml `publisher:` overrides it for one job
    "fake_publisher": {"state_path": None},   # None -> <vp_home>/fake_publisher.json
    "youtube": {
        "client_secrets_path": None,   # None -> <vp_home>/youtube_client_secret.json
        "token_path": None,            # None -> <vp_home>/youtube_token.json
        "channel_id": None,            # filled by `vp youtube authorize`
        "expected_channel_handle": None,  # filled by authorize when empty
        "category_id": "27",        # Education
        "language": "ru",
        "chunk_size_mb": 8,         # resumable upload chunk
        "uploads_scan_pages": 4,    # find_by_key: pages of the uploads playlist
        "playlist_scan_pages": 10,  # ensure_playlist: pages of the channel's playlists
    },
    "notifier": "session",          # session (log only; gates in the dialog) | file | telegram
    "telegram": {"token_env": "VP_TELEGRAM_TOKEN", "chat_id_env": "VP_TELEGRAM_CHAT_ID"},
    "renderer": "ffmpeg",
    "budget": {"max_usd_per_run": 20.0, "youtube_quota_per_day": 10000},
    "retries": {"transport": 3, "backoff_s": [2, 5, 15]},
    "loop_guard": {"max_failures": 3},
    # S3a prompt builder (vp.knowledge.prompt_builder.DEFAULTS)
    "selection": {"k_cases": 6, "case_chars_budget": 6000, "case_text_max": 700},
    # S5/S7 renderer (vp.adapters.render.BRAND_DEFAULTS / RENDER_DEFAULTS)
    "brand": {
        "channel_plate": None,      # the plate on videos; filled by authorize when empty
        "font_file": None,
        "font_name": "Arial",
        "text_color": "#FFFFFF",
        "plate_color": "#000000",
        "card_bg": "#14181F",
        "card_text_color": "#FFFFFF",
    },
    "render": {
        "cards": {
            "reel": {"intro_ms": 1500, "outro_ms": 1500},
            "long": {"intro_ms": 3000, "outro_ms": 4000},
            "full": {"intro_ms": 3000, "outro_ms": 4000},
        },
        "lufs": -14.0,
        "lufs_tolerance": 1.0,
        "true_peak": -1.0,
        "lra": 11.0,
        "card_font_frac": 0.075,
        "plate_font_frac": 0.045,
        "reel_sub_font_size": 16,
        "reel_sub_margin_v": 40,
        "thumb_font_px": 72,
        "thumb_max_bytes": 2 * 1024 * 1024,
    },
    # S6b (vp.steps.s6b_metadata.DEFAULTS)
    "s6b": {"description_reserve": 600, "tags_reserve": 20, "min_chapters": 3,
            "chapter_min_gap_s": 10, "category_id": "27", "language": "ru", "max_hashtags": 3},
    # S7 (vp.steps.s7_thumbnail.DEFAULTS)
    "s7": {"candidates": 8, "top": 4},
    # S8 (vp.steps.s8_upload._upload_cfg)
    "upload": {"find_attempts": 3, "find_pause_s": 30.0, "processing_polls": 40,
               "processing_pause_s": 15.0},
    # S9 approval (vp.steps.s9_package.approval_cfg)
    "approval": {"timezone": "UTC",
                 "schedule_slots": [{"label": "завтра 10:00", "day_offset": 1, "time": "10:00"}]},
    "s9": {"nuance": False},
    "s10": {"max_links": 5},
    # S3c learning loop (vp.knowledge.learn.LEARN_DEFAULTS)
    "learn": {
        "min_independent_cases": 3,
        "min_recordings_for_generalisation": 2,
        "min_fix_recordings": 1,
        "min_holdout_recordings": 1,
        "candidate_eval_threshold": 0.10,
        "observe_min_recordings": 2,
        "tolerance": {"precision": 0.0, "boundary_error_s": 0.5, "misses_per_recording": 0.0,
                      "candidates_per_recording": 0.0, "adjust_share": 0.0,
                      "review_minutes_per_recording": 1.0},
        "budget": {"max_prompt_chars": 60000, "max_prompt_growth": 0.25,
                   "max_cost_per_recording_min_usd": None, "max_cost_growth": 0.25},
        "critic": {"max_groups": 30, "examples_per_group": 3, "text_chars": 300, "max_findings": 8},
        "analyst": {"max_candidates": 5, "rejected_tail": 20, "history_versions": 6, "text_chars": 300},
    },
    "profiles": {
        "lecture": copy.deepcopy(_PROFILE_BASE),
        "interview": copy.deepcopy(_PROFILE_BASE),
        "qa": copy.deepcopy(_PROFILE_BASE),
    },
}


PUBLISHERS = ("youtube", "fake")
YOUTUBE_CLIENT_FILE = "youtube_client_secret.json"   # default name in <vp_home>
YOUTUBE_TOKEN_FILE = "youtube_token.json"
# <plugin root>/assets/youtube_client.json (this file is <root>/src/vp/core/config.py)
BUNDLED_YOUTUBE_CLIENT = Path(__file__).resolve().parents[3] / "assets" / "youtube_client.json"
BRIEF_MAX_CHARS = 2000              # request.yaml `brief`, counted after stripping


def deep_merge(base: Any, over: Any) -> Any:
    """Return a new structure: dicts merged recursively, anything else replaced by `over`."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = {k: copy.deepcopy(v) for k, v in base.items()}
        for k, v in over.items():
            out[k] = deep_merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def config_path(home: Path | None = None) -> Path:
    return (home or vp_home()) / "config.yaml"


def load_config(home: str | os.PathLike | None = None) -> dict[str, Any]:
    """Load <vp_home>/config.yaml merged over DEFAULTS; writes the defaults file if absent.

    The returned dict also carries `vp_home` (absolute path string).
    """
    h = Path(home).expanduser() if home else vp_home()
    path = config_path(h)
    if not path.exists():
        write_yaml(path, DEFAULTS)
    user = read_yaml(path, default={})
    if not isinstance(user, dict):
        raise VPError("data", f"{path}: config must be a mapping")
    cfg = deep_merge(DEFAULTS, user)
    cfg["vp_home"] = str(h.resolve())
    # A new user only drops the Google client file into <vp_home>: the YouTube paths default
    # there (computed, not written to config.yaml; an explicit path in config.yaml wins).
    yt = cfg["youtube"]
    own_client = bool(yt.get("client_secrets_path"))
    for key, name in (("client_secrets_path", YOUTUBE_CLIENT_FILE), ("token_path", YOUTUBE_TOKEN_FILE)):
        if not yt.get(key):
            yt[key] = str(h.resolve() / name)
    # The plugin ships its own Google client (a desktop app's client is not a secret by Google's
    # design: the user's channel access is the token, which stays on this machine). An explicit
    # path in config.yaml, or a file the user put into <vp_home>, wins over the bundled one.
    if (not own_client and not Path(yt["client_secrets_path"]).is_file()
            and BUNDLED_YOUTUBE_CLIENT.is_file()):
        yt["client_secrets_path"] = str(BUNDLED_YOUTUBE_CLIENT)
    if os.environ.get("VP_JUDGE"):
        cfg["judge"] = os.environ["VP_JUDGE"]
    return cfg


def resolve_job_config(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Config for one job: adds `profile_name` and `profile` (profile + request `overrides`);
    request `publisher: fake|youtube` overrides the publisher for this job only.

    Raises VPError(data) for an unknown profile or publisher.
    """
    name = (request or {}).get("profile") or "lecture"
    profiles = config.get("profiles") or {}
    if name not in profiles:
        raise VPError("data", f"unknown profile {name!r}; known: {sorted(profiles)}")
    cfg = copy.deepcopy(config)
    pub = (request or {}).get("publisher")
    if pub is not None:
        if pub not in PUBLISHERS:
            raise VPError("data", f"request.yaml publisher must be one of {list(PUBLISHERS)}, "
                                  f"got {pub!r}")
        cfg["publisher"] = pub
        cfg["publisher_source"] = "request"
    cfg["profile_name"] = name
    cfg["profile"] = deep_merge(profiles[name], (request or {}).get("overrides") or {})
    return cfg


def request_brief(request: dict[str, Any] | None) -> str | None:
    """request.yaml `brief` — the director's task for this recording, free text (optional): who
    the videos are for, why, what to look for, what to avoid. It reaches the S3a selection prompt.

    Returns the text without surrounding whitespace, or None when the key is absent or the text
    is empty after stripping. Raises VPError(data) when the value is not a string or is longer
    than BRIEF_MAX_CHARS — a too long brief is refused, never cut silently. S0 calls this to stop
    the route early; S3a calls it for the value (vp.steps.s3a_select.job_brief).
    """
    raw = (request or {}).get("brief")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise VPError("data", f"request.yaml brief must be text (a string), "
                              f"got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        return None
    if len(text) > BRIEF_MAX_CHARS:
        raise VPError("data", f"request.yaml brief is too long: {len(text)} characters, the "
                              f"limit is {BRIEF_MAX_CHARS}. Shorten it and run again.")
    return text


def set_config_value(section: str, key: str, value: Any, home: str | os.PathLike | None = None) -> Path:
    """Set `<section>.<key>` in <vp_home>/config.yaml and leave everything else as it is (comments,
    order, other keys). The value line is edited in place in the text; when the file layout does not
    allow that (flow style, odd indentation), the YAML is rewritten with only this key changed.
    The result is verified by parsing it back. Returns the config path."""
    import re

    import yaml

    from .io import atomic_write, dump_yaml

    h = Path(home).expanduser() if home else vp_home()
    path = config_path(h)
    if not path.exists():
        write_yaml(path, DEFAULTS)
    text = path.read_text(encoding="utf-8")
    before = yaml.safe_load(text) or {}
    if not isinstance(before, dict):
        raise VPError("data", f"{path}: config must be a mapping")
    want = copy.deepcopy(before)
    if not isinstance(want.get(section), dict):
        want[section] = {} if want.get(section) is None else want[section]
    if not isinstance(want[section], dict):
        raise VPError("data", f"{path}: `{section}` must be a mapping")
    want[section][key] = value

    scalar = json_scalar(value)
    lines = text.splitlines(keepends=True)
    new_text = None
    head = next((i for i, l in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", l)), None)
    if head is None and section not in before:
        sep = "" if not text or text.endswith("\n") else "\n"
        new_text = f"{text}{sep}{section}:\n  {key}: {scalar}\n"
    elif head is not None:
        end, indent = len(lines), None
        for j in range(head + 1, len(lines)):
            ln = lines[j]
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            m = re.match(r"^(\s+)", ln)
            if not m:
                end = j
                break
            indent = indent or m.group(1)
        indent = indent or "  "
        pat = re.compile(rf"^{re.escape(indent)}{re.escape(key)}:(\s*)([^#\n]*?)(\s+#[^\n]*)?(\r?\n)?$")
        for j in range(head + 1, end):
            m = pat.match(lines[j])
            if m:
                lines[j] = f"{indent}{key}: {scalar}{m.group(3) or ''}{m.group(4) or ''}"
                new_text = "".join(lines)
                break
        else:
            lines.insert(head + 1, f"{indent}{key}: {scalar}\n")
            new_text = "".join(lines)
    try:
        ok = new_text is not None and yaml.safe_load(new_text) == want
    except yaml.YAMLError:
        ok = False
    atomic_write(path, new_text if ok else dump_yaml(want))
    return path


def json_scalar(value: Any) -> str:
    """A YAML scalar for a simple value (strings double-quoted, JSON-compatible)."""
    import json
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)
