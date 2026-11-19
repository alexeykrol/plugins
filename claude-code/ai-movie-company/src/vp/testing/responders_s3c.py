"""Default fake responders for S3c: `s3c.critic`, `s3c.analyst`, `s3c.replay.<rec>.<type>`.

They produce VALID outputs from their real inputs so an end-to-end learn cycle runs offline:
  critic   one finding per error group (reject / adjust / added) with all its keys as evidence
  analyst  per finding: a new rule (sources = evidence) and a new case from the first evidence
  replay   the S3a fake selection (real quotes from the transcript)
"""
from __future__ import annotations

import yaml

from .fake_responders import register
from .responders_s3 import respond_select

TITLES = {"reject": "Повторяющийся отказ", "adjust": "Неточные границы", "added": "Пропуск сильного места"}


def respond_critic(task: str, prompt: str, inputs: dict[str, str]) -> str:
    doc = yaml.safe_load(inputs.get("verdict_groups.yaml") or "") or {}
    findings = []
    for g in doc.get("groups") or []:
        if g.get("verdict") not in TITLES or not g.get("keys"):
            continue
        findings.append({"title": f"{TITLES[g['verdict']]} ({g.get('type')})",
                         "type": g.get("type") if g.get("type") in ("reel", "long") else "any",
                         "reason_class": g.get("reason_class"),
                         "rules": [r for r in (g.get("rules_cited") or {})][:3],
                         "evidence": list(g["keys"]),
                         "likely_cause": "Критерии не различают этот класс случаев."})
    return yaml.safe_dump({"findings": findings[:8]}, allow_unicode=True, sort_keys=False)


def respond_analyst(task: str, prompt: str, inputs: dict[str, str]) -> str:
    fs = (yaml.safe_load(inputs.get("findings.yaml") or "") or {}).get("findings") or []
    vs = {v["key"]: v for v in (yaml.safe_load(inputs.get("verdicts.yaml") or "") or {}).get("verdicts") or []}
    cands = []
    for f in fs:
        ev = [k for k in f.get("evidence") or [] if k in vs]
        if not ev:
            continue
        typ = f.get("type") if f.get("type") in ("reel", "long") else None
        cands.append({"kind": "new_rule", "hypothesis": f"Правило против паттерна «{f['title']}».",
                      "expected_effect": "Точность растёт, доля reject падает.", "sources": ev,
                      "edit": {"rule": {"applies_to": [typ] if typ else ["reel", "long"],
                                        "kind": "forbid", "text": f"Избегать: {f['title']}"}}})
        first = vs[ev[0]]
        cands.append({"kind": "new_case", "hypothesis": f"Пример к паттерну «{f['title']}».",
                      "expected_effect": "Модель видит образец ошибки.", "sources": [ev[0]],
                      "edit": {"source": ev[0],
                               "label": "positive" if first.get("verdict") in ("added", "accept") else "negative",
                               "lesson": f"Урок: {f['likely_cause']}"}})
    return yaml.safe_dump({"candidates": cands[:4]}, allow_unicode=True, sort_keys=False)


register("s3c.critic", respond_critic)
register("s3c.analyst", respond_analyst)
register("s3c.replay.", respond_select)
