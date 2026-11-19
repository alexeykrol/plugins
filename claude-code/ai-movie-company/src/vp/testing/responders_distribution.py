"""Default fake responders for the distribution steps (tests and offline e2e).

s10.graph   full -> up to 5 fragments; each fragment -> its neighbours in recording order
s9.nuance   always "OK"
"""
from __future__ import annotations

import json
import re

from .fake_responders import register

_ITEM_RE = re.compile(r"^\[(\d+)\] type: (\w+) \| order: (\d+)", re.M)


def s10_graph(task: str, prompt: str, inputs: dict[str, str]) -> str:
    items = [(int(n), kind, int(order)) for n, kind, order in _ITEM_RE.findall(inputs.get("items", ""))]
    frags = sorted((i for i in items if i[0] != 0), key=lambda i: i[2])
    links = [{"from": 0, "to": n, "kind": "fragment"} for n, _k, _o in frags[:5]]
    for pos, (n, _k, _o) in enumerate(frags):
        if pos + 1 < len(frags):
            links.append({"from": n, "to": frags[pos + 1][0], "kind": "next"})
        if pos > 0:
            links.append({"from": n, "to": frags[pos - 1][0], "kind": "prev"})
    return json.dumps({"links": links})


def s9_nuance(task: str, prompt: str, inputs: dict[str, str]) -> str:
    return "OK"


register("s10.graph", s10_graph)
register("s9.nuance", s9_nuance)
