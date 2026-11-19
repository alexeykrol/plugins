"""Ctx — everything a step gets (BUILD §3): job, item, config, judge, publisher, notifier,
renderer, log, costs.

Services are created lazily on first access from the config, so a step that never touches
YouTube never imports the Google client. Tests pass ready objects instead.
"""
from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, Callable

from .costs import CostJournal
from .errors import VPError, with_retries
from .log import Logger
from .paths import Job

_SERVICES = ("judge", "publisher", "notifier", "renderer")


class Ctx:
    """Execution context of one step instance.

    Attributes: job (Job), item (str | None), config (dict; resolved per job, has `profile`),
    log (Logger), costs (CostJournal); lazy properties judge, publisher, notifier, renderer.
    """

    def __init__(self, job: Job, item: str | None = None, config: dict[str, Any] | None = None,
                 *, judge: Any = None, publisher: Any = None, notifier: Any = None,
                 renderer: Any = None, log: Logger | None = None, costs: CostJournal | None = None,
                 factories: dict[str, Callable[["Ctx"], Any]] | None = None,
                 _shared: dict[str, Any] | None = None):
        self.job = job
        self.item = item
        self.config: dict[str, Any] = config or {}
        home = self.config.get("vp_home") or str(Path.home() / ".ai_video_pipeline")
        self.log = log or Logger(home, job.job_id)
        self.costs = costs or CostJournal(home, job.job_id)
        # shared service cache: instances created once per run, shared by all item contexts
        self._shared = _shared if _shared is not None else {}
        for name, obj in (("judge", judge), ("publisher", publisher), ("notifier", notifier),
                          ("renderer", renderer)):
            if obj is not None:
                self._shared[name] = obj
        self._factories = dict(DEFAULT_FACTORIES)
        self._factories.update(factories or {})

    def _service(self, name: str) -> Any:
        if name not in self._shared:
            self._shared[name] = self._factories[name](self)
        return self._shared[name]

    @property
    def judge(self) -> Any: return self._service("judge")
    @property
    def publisher(self) -> Any: return self._service("publisher")
    @property
    def notifier(self) -> Any: return self._service("notifier")
    @property
    def renderer(self) -> Any: return self._service("renderer")

    @property
    def publisher_kind(self) -> str:
        """"youtube" | "fake": the name of the publisher actually used (an injected object wins
        over the config; request.yaml `publisher:` is already in the job config)."""
        obj = self._shared.get("publisher")
        name = getattr(obj, "name", None) if obj is not None else None
        return str(name or self.config.get("publisher", "youtube"))

    @property
    def profile(self) -> dict[str, Any]:
        """Resolved profile of the job (profile + request overrides)."""
        return self.config.get("profile") or {}

    @property
    def home(self) -> Path:
        return Path(self.config.get("vp_home") or Path.home() / ".ai_video_pipeline")

    def for_item(self, item: str | None) -> "Ctx":
        """Same job/config/services, another item."""
        c = Ctx(self.job, item, self.config, log=self.log, costs=self.costs,
                _shared=self._shared)
        c._factories = self._factories
        c.sleep = self.sleep
        return c

    sleep: Callable[[float], None] = staticmethod(time.sleep)

    def retry(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` with transport retries from config `retries` (default 3 retries, backoff 2/5/15 s).

        Steps wrap every network call in this. Auth/data/contract errors propagate at once.
        """
        r = self.config.get("retries") or {}
        return with_retries(fn, n=int(r.get("transport", 3)), backoff=r.get("backoff_s") or (2, 5, 15),
                            sleep=self.sleep,
                            on_retry=lambda i, e: self.log.event("retry", attempt=i, error=str(e)))


# ---- default factories (from config) -------------------------------------------------------

def _load_attr(path: str) -> Any:
    mod, _, attr = path.partition(":")
    try:
        module = importlib.import_module(mod)
    except ModuleNotFoundError as e:
        raise VPError("internal", f"{mod} is not implemented yet ({e.name})") from e
    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise VPError("internal", f"{path} is not implemented yet") from e


def _make_judge(ctx: Ctx) -> Any:
    from .judge import make_judge
    return make_judge(ctx.config)


def _make_publisher(ctx: Ctx) -> Any:
    kind = ctx.config.get("publisher", "youtube")
    if kind == "fake":
        from ..adapters.fake_publisher import FakePublisher
        path = (ctx.config.get("fake_publisher") or {}).get("state_path") or ctx.home / "fake_publisher.json"
        return FakePublisher(path, costs=ctx.costs)
    if kind == "youtube":
        cls = _load_attr("vp.adapters.youtube:YouTubePublisher")
        return cls(ctx.config.get("youtube") or {}, costs=ctx.costs)
    raise VPError("data", f"unknown publisher {kind!r}")


def _make_notifier(ctx: Ctx) -> Any:
    kind = ctx.config.get("notifier", "session")
    from ..adapters.notifier import FileNotifier, SessionNotifier, TelegramNotifier
    if kind == "session":
        return SessionNotifier(ctx.home)
    if kind == "file":
        return FileNotifier(ctx.home)
    if kind == "telegram":
        tg = ctx.config.get("telegram") or {}
        return TelegramNotifier(tg.get("token_env", "VP_TELEGRAM_TOKEN"),
                                tg.get("chat_id_env", "VP_TELEGRAM_CHAT_ID"), home=ctx.home)
    raise VPError("data", f"unknown notifier {kind!r}")


def _make_renderer(ctx: Ctx) -> Any:
    kind = ctx.config.get("renderer", "ffmpeg")
    if kind == "ffmpeg":
        return _load_attr("vp.adapters.render:FfmpegRenderer")(ctx.config)
    raise VPError("data", f"unknown renderer {kind!r}")


DEFAULT_FACTORIES: dict[str, Callable[[Ctx], Any]] = {
    "judge": _make_judge,
    "publisher": _make_publisher,
    "notifier": _make_notifier,
    "renderer": _make_renderer,
}
