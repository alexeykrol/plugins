"""Version 1 reel policy, transcribed from the owner rules in the builders design."""
from dataclasses import dataclass
from math import sqrt
from types import MappingProxyType
from typing import TypedDict


@dataclass(frozen=True)
class ReelPreset:
    width: int
    height: int
    revision: str
    split_speaker_height_px: int | None
    split_screen_height_px: int | None
    caption_clearance_px: int | None
    caption_half_extra_px: int | None
    screen_fit_tolerance_px: int | None

    # §1: "810×1440 at 25 fps" and "1080×1920 at 25 fps".
    fps: int = 25
    # §1: "Hold the hook card for **3,000 ms**" and "`0.45 × card frames`".
    intro_ms: int = 3000
    intro_reveal_fraction: float = 0.45
    # §1: "Show **1,000 ms** of picture before speech."
    # "Fade picture in over **300 ms** and open the sound over **80 ms**".
    quiet_lead_ms: int = 1000
    picture_fade_in_ms: int = 300
    audio_open_ms: int = 80
    # §1: "**200 ms** pauses, then **100 ms** gaps at **−32 dB**, with a **60 ms** pre-word fallback."
    pause_lengths_ms: tuple[int, int] = (200, 100)
    silence_db: int = -32
    pre_word_fallback_ms: int = 60
    # §1: "add **500 ms** of picture, and fade to black over the last **400 ms**."
    tail_picture_ms: int = 500
    picture_fade_out_ms: int = 400
    # §1: "searches for **200/100 ms** pauses within **800/500 ms** of the word end".
    tail_pause_windows_ms: tuple[int, int] = (800, 500)
    # §1: "a centered main thought for **5,000 ms**" and "revealed at **50%** of the card."
    thought_ms: int = 5000
    subline_reveal_fraction: float = 0.50
    # 2026-11-23 morning Zoom job, fx/build_w2.py:94–99 and fx/build_v3.py:66.
    # Gaussian sigma of equal variance to boxblur=30:3 (radius 30, three passes).
    medium_background_blur_sigma: float = sqrt(3 * ((2 * 30 + 1) ** 2 - 1) / 12)
    medium_background_brightness: float = -0.18
    # §1: "Speaker above a **6 px** yellow divider and shared screen below."
    divider_px: int = 6
    # §1: "a no-upscale rule for the speaker".
    allow_speaker_upscale: bool = False
    # §1: "switches crops **300 ms** before a specified word".
    crop_switch_lead_ms: int = 300
    # §1: "at least half" of aligned word midpoints; "**250 ms** inner handles and **120 ms** inner video fades".
    cue_word_share: float = 0.5
    inner_handle_ms: int = 250
    inner_picture_fade_ms: int = 120
    # §1: "pages of **3 words / 18 characters**" and "a pause of about **700 ms**".
    caption_page_words: int = 3
    caption_page_chars: int = 18
    caption_pause_break_ms: int = 700
    # §1: "**0.72** of frame height for a filled screen and **0.76** otherwise".
    caption_filled_screen_y_frac: float = 0.72
    caption_other_y_frac: float = 0.76
    # §1: "start the effect **120 ms** before it", "**1,500 ms** of lookup slack",
    # and "clamp the start to **500 ms** before clip end."
    effect_lead_ms: int = 120
    effect_lookup_slack_ms: int = 1500
    effect_latest_start_from_end_ms: int = 500
    # §1: "`CAP_FONT=0.085×W`, and `CAP_HALF=1.12×CAP_FONT+22`."
    caption_font_frac: float = 0.085
    caption_half_font_factor: float = 1.12
    # §1: "Tags sit around `y=0.44` or `0.40`, stickers beside the head around `x=0.15/0.85, y=0.17`."
    tag_y_fractions: tuple[float, float] = (0.44, 0.40)
    sticker_x_fractions: tuple[float, float] = (0.15, 0.85)
    sticker_y_frac: float = 0.17
    # §1: "Tags shrink to fit **0.86×W**. The review softened two zooms from **1.14** to **1.08**."
    tag_max_width_frac: float = 0.86
    split_zoom_scale: float = 1.08
    # §1: "**−14 ±0.15 LUFS** and **≤ −1 dBTP** after AAC",
    # "up to **six** retries from the same pre-master, a **192 kHz** limiter pass".
    target_lufs: int = -14
    lufs_tolerance: float = 0.15
    max_dbtp: int = -1
    mastering_retries: int = 6
    limiter_sample_rate: int = 192_000
    # §1: "audio lag on a **5 ms** grid within **±300 ms**" and "**540×960** chat previews".
    audio_lag_grid_ms: int = 5
    audio_lag_search_ms: int = 300
    preview_size: tuple[int, int] = (540, 960)


# §1: "1080×1920 at 25 fps", "1296×720 → 1080×600" and "1080×1314 screen band".
# §1: "60 px caption clearance at 1080-wide" and "`CAP_HALF=1.12×CAP_FONT+22`."
# §1: "a **40 px** fit tolerance" in the later 1080-wide job.
owner_reel_1080 = ReelPreset(
    width=1080, height=1920, revision="1",
    split_speaker_height_px=600, split_screen_height_px=1314,
    caption_clearance_px=60, caption_half_extra_px=22, screen_fit_tolerance_px=40,
)

# §1: "810×1440 at 25 fps", with a source-specific "1:1 split speaker crop".
# Output band heights and pixel clearances for this canvas require a policy decision.
owner_reel_810 = ReelPreset(
    width=810, height=1440, revision="1",
    split_speaker_height_px=None, split_screen_height_px=None,
    caption_clearance_px=None, caption_half_extra_px=None, screen_fit_tolerance_px=None,
)

DEFAULT_REEL_PRESET = "owner_reel_1080"
REEL_PRESETS = MappingProxyType({
    DEFAULT_REEL_PRESET: owner_reel_1080,
    "owner_reel_810": owner_reel_810,
})


class RequiredReelSettings(TypedDict):
    """Effective settings supplied by the build policy where §1 gives no value."""

    sample_rate: int
    intro_readable_dwell_frames: int
    thought_readable_dwell_frames: int
    default_effect_duration_ms: dict[str, int]
    max_audio_lag_ms: int


class RequiredReel810Geometry(TypedDict):
    """Pixel settings needed by the 810 layout and geometry guard."""

    split_speaker_height_px: int
    split_screen_height_px: int
    caption_clearance_px: int
    caption_half_extra_px: int
    screen_fit_tolerance_px: int
