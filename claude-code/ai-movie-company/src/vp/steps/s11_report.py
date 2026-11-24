"""S11 — report (SPEC S11, §7.4, §7.9). Code. Job scope, last step.

report.md (owner-facing, Russian data): what was done; what was rejected and why (owner reject,
review violations, revoked at publish, uncertain upload, deferred schedule); costs of the job
(model tokens and USD, YouTube quota units); links; human time by kind from status.yaml
(`review` — the central process metric — per recording and per published video; `full_scan`
and `candidate_eval` separately, never mixed in); the person's requests still owed (asks.yaml, only
when the job has a ledger). Publish times per video are in uploads.yaml
(`published.published_at`, SPEC §11) and listed here.

The owner is notified once per distinct report (an identical rerun sends nothing).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..asks import report_lines as asks_lines
from ..core.io import atomic_write
from ..core.paths import FULL
from ..core.status import Status
from . import _results
from .base import Ctx, StepSpec

HUMAN_KINDS = ("review", "full_scan", "candidate_eval")


def human_time(ctx: Ctx) -> dict[str, dict[str, Any]]:
    """Minutes by kind from status.yaml `human_time` entries (+ how many are proxies)."""
    out = {k: {"minutes": 0.0, "gates": 0, "proxy": 0} for k in HUMAN_KINDS}
    for e in Status(ctx.job).history():
        ht = (e.get("metadata") or {}).get("human_time")
        if not isinstance(ht, dict):
            continue
        k = ht.get("kind", "review")
        if k not in out:
            continue
        out[k]["minutes"] += float(ht.get("minutes") or 0)
        out[k]["gates"] += 1
        out[k]["proxy"] += 1 if ht.get("source") == "proxy" else 0
    for v in out.values():
        v["minutes"] = round(v["minutes"], 1)
    return out


def costs(ctx: Ctx) -> dict[str, Any]:
    recs = ctx.costs.records(ctx.job.job_id)
    quota = sum(int(r.get("units") or 0) for r in recs if r.get("kind") == "youtube_quota")
    judge = [r for r in recs if r.get("kind") == "judge"]
    return {"usd": round(sum(float(r.get("usd") or 0) for r in recs), 4),
            "judge_calls": len(judge),
            "input_tokens": sum(int(r.get("input_tokens") or 0) for r in judge),
            "output_tokens": sum(int(r.get("output_tokens") or 0) for r in judge),
            "youtube_quota_units": quota}


def item_outcomes(ctx: Ctx) -> list[dict[str, Any]]:
    return [{k: v for k, v in o.items() if k != "entry"} for o in _results.outcomes(ctx.job)]


REHEARSAL_BANNER = "> **РЕПЕТИЦИЯ — на YouTube ничего не загружено.** Ссылки ниже — локальные файлы."


def render_report(ctx: Ctx) -> str:
    rows = item_outcomes(ctx)
    fake = _results.job_publisher(ctx.job, ctx.config) == "fake"
    rh = " (репетиция)" if fake else ""
    c = costs(ctx)
    ht = human_time(ctx)
    n = {s: sum(1 for r in rows if r["state"] == s) for s in
         ("published", "scheduled", "rejected", "violations", "revoked", "deferred",
          "not_uploaded", "waiting")}
    accepted = n["published"] + n["scheduled"]
    lines = [f"# Отчёт vp: {ctx.job.job_id}", ""]
    if fake:
        lines += [REHEARSAL_BANNER, ""]
    breakdown = f" (фрагментов {len(rows) - 1} + полное видео)" if any(
        r["item"] == FULL for r in rows) else ""
    lines += [f"Роликов: {len(rows)}{breakdown}. "
              f"Опубликовано{rh}: {n['published']}, запланировано{rh}: {n['scheduled']}, отклонено: {n['rejected']}, "
              f"с нарушениями: {n['violations']}, возвращено на одобрение: {n['revoked']}, "
              f"отложено: {n['deferred']}, не загружено: {n['not_uploaded']}, без решения: {n['waiting']}.",
              "", "## Ролики", "",
              "| Ролик | Тип | Название | Итог | Публикация | " + ("Файл |" if fake else "Ссылка |"),
              "|---|---|---|---|---|---|"]
    for r in rows:
        title = (r["title"] or "").replace("|", "/")
        state = r["state"] + (rh if r["state"] in ("published", "scheduled") else "")
        where = _where(ctx, r, fake)
        lines.append(f"| {r['item']} | {r['kind'] or ''} | {title} | {state} | "
                     f"{r['published_at'] or ''} | {where} |")
    bad = [r for r in rows if r["why"]]
    lines += ["", "## Не опубликовано и почему", ""]
    lines += [f"- {r['item']}: {r['state']} — {r['why']}" for r in bad] or ["- нет"]
    lines += ["", "## Расходы", "",
              f"- модель: {c['judge_calls']} вызовов, токены вход/выход {c['input_tokens']}/"
              f"{c['output_tokens']}, ${c['usd']}"]
    if not fake:
        lines.append(f"- квота YouTube: {c['youtube_quota_units']} единиц")
    rv = ht["review"]
    per_video = round(rv["minutes"] / accepted, 1) if accepted else None
    lines += ["", "## Время человека", "",
              f"- review (центральная метрика): {rv['minutes']} мин на запись"
              + (f", {per_video} мин на принятый ролик" if per_video is not None else "")
              + (f" (прокси в {rv['proxy']} из {rv['gates']} гейтов)" if rv["proxy"] else ""),
              f"- full_scan (обучение, отдельно): {ht['full_scan']['minutes']} мин",
              f"- candidate_eval (обучение, отдельно): {ht['candidate_eval']['minutes']} мин"]
    if fake:
        files = [(r["item"], _where(ctx, r, True)) for r in rows]
        lines += ["", "## Файлы (репетиция)", ""] + [f"- {i}: {w}" for i, w in files if w]
    else:
        links = [r for r in rows if r["url"]]
        lines += ["", "## Ссылки", ""] + [f"- {r['item']}: {r['url']}" for r in links]
    lines += asks_lines(ctx.job)
    return "\n".join(lines) + "\n"


def _where(ctx: Ctx, row: dict[str, Any], fake: bool) -> str:
    if not fake:
        return row["url"] or ""
    p = ctx.job.render_mp4(row["item"])
    return str(p.resolve()) if p.exists() else ""


class S11Report(StepSpec):
    id, deps, scope, version, gate = "S11", ("PUB",), "job", "1", None

    def inputs(self, ctx: Ctx) -> list[Path]:
        j = ctx.job
        return [j.uploads_yaml, j.approval_yaml, j.review_yaml, j.crosslinks_yaml, j.asks_yaml]

    def outputs(self, ctx: Ctx) -> list[Path]:
        return [ctx.job.report_md]

    def run(self, ctx: Ctx) -> dict:
        text = render_report(ctx)
        path = ctx.job.report_md
        same = path.exists() and path.read_text(encoding="utf-8") == text
        if not same:
            atomic_write(path, text)
            rows = item_outcomes(ctx)
            pub = sum(1 for r in rows if r["state"] in ("published", "scheduled"))
            if _results.job_publisher(ctx.job, ctx.config) == "fake":
                text = (f"vp {ctx.job.job_id}: репетиция завершена — на YouTube ничего не загружено "
                        f"({pub} из {len(rows)} прошли бы публикацию). Отчёт: {path}")
            else:
                text = (f"vp {ctx.job.job_id}: готово — опубликовано/запланировано {pub} "
                        f"из {len(rows)}. Отчёт: {path}")
            try:
                ctx.notifier.send(text)
            except Exception as e:
                ctx.log.event("notify_failed", step="S11", error=type(e).__name__)
        return {"notified": not same}


STEP = S11Report()
