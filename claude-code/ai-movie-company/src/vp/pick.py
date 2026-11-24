"""Open a native macOS chooser and return the selection on stdout."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence


def _literal(value: str) -> str:
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))
    return '"' + escaped + '"'


def _choose(kind: str, prompt: str | None, start: str | None,
            types: list[str], timeout: int) -> dict[str, bool | str]:
    if sys.platform != "darwin":
        return {"picked": False, "reason": "unsupported"}

    command = f"choose {kind}"
    if prompt is not None:
        command += f" with prompt {_literal(prompt)}"
    if types:
        command += " of type {" + ", ".join(map(_literal, types)) + "}"
    directory = os.path.abspath(os.path.expanduser(start)) if start else ""
    if os.path.isdir(directory):        # a missing start folder is not an error: open the default place
        command += f" default location ((POSIX file {_literal(directory)}) as alias)"
    script = f"activate\nreturn POSIX path of ({command})\n"

    try:
        result = subprocess.run(["osascript", "-"], input=script.encode("utf-8"),
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"picked": False, "reason": "timeout"}
    except OSError:
        return {"picked": False, "reason": "no_gui"}

    if result.returncode:
        reason = "cancelled" if result.stderr.rstrip().endswith(b"(-128)") else "no_gui"
        return {"picked": False, "reason": reason}
    # Remove osascript's output terminator while preserving whitespace in the selected path.
    path = result.stdout.decode("utf-8").removesuffix("\n")
    return {"picked": True, "path": path}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vp pick", description=__doc__)
    parser.add_argument("kind", choices=("folder", "file"))
    parser.add_argument("--prompt", help="dialog prompt in the person's language")
    parser.add_argument("--types", metavar="EXTS", help="comma-separated file extensions")
    parser.add_argument("--start", metavar="DIR", help="initial directory")
    parser.add_argument("--timeout", type=int, default=300, metavar="SECONDS")
    parser.add_argument("--json", action="store_true", help="output JSON (the default)")
    args = parser.parse_args(argv)
    if args.types is not None and args.kind != "file":
        parser.error("--types is only available with pick file")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    types = [item.strip() for item in (args.types or "").split(",") if item.strip()]
    print(json.dumps(_choose(args.kind, args.prompt, args.start, types, args.timeout)))
    return 0
