"""Commands for inspecting and refreshing knowledge and browsing its cases."""
import argparse
import json

from . import _content, _read_state, _root, _usable_pack, load, rollback, status, sync


def _show():
    root = _root()
    pack = _usable_pack(root, _read_state(root), None)
    content = _content(pack)
    print(f"Knowledge pack v{pack['version']}, created {pack['created']}" if pack else "Release baseline")
    print("Scenarios: " + ", ".join(f"{s['id']} ({s['name']['en']})" for s in content["scenarios"]))
    for case in content["cases"]:
        print(f"Case {case['id']}: {case['title']['en']}")
    for rule in content["rules"]:
        print(f"Rule {rule['id']} ({rule['applies_to']}): {rule['text']['en']}")
    for key, text in content["hints"].items():
        print(f"Hint {key}: {text['en']}")


def main(argv):
    ap = argparse.ArgumentParser(prog="vp kpack", description="Manage the signed knowledge pack")
    ap.add_argument("action", choices=("status", "sync", "rollback", "show"))
    args = ap.parse_args(argv)
    if args.action == "sync":
        result = sync()
        if result == "skipped_terms":
            print("Knowledge pack sync is off until the new terms are accepted: no request was made. "
                  "The knowledge built into this release is in use.")
            return 0
        print(f"Knowledge pack sync: {result}")
        return 0 if result in {"activated", "unchanged", "ignored_version",
                               "skipped_plugin_version"} else 1
    if args.action == "rollback":
        try:
            version = rollback()
        except OSError as exc:
            print(f"Knowledge pack rollback failed: {type(exc).__name__}")
            return 1
        if version is None:
            print("No active knowledge pack to roll back")
            return 1
        print(f"Knowledge pack v{version} restored" if version else "Release baseline is active")
    elif args.action == "show":
        _show()
    else:
        state = status()
        print(f"Effective knowledge version: {state['effective'] or 'release baseline'}")
        for key in ("active", "previous", "pinned_below", "last_sync_at", "last_error"):
            print(f"{key}: {state.get(key) if state.get(key) is not None else '—'}")
    return 0


def case_main(argv):
    ap = argparse.ArgumentParser(prog="vp case", description="Browse effective cases")
    sub = ap.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("id")
    for command in (listing, show):
        command.add_argument("--lang", choices=("en", "ru"), default="en")
        command.add_argument("--json", action="store_true", help="emit complete case data")
    args = ap.parse_args(argv)
    cases = load()["cases"]
    if args.action == "list":
        if args.json:
            print(json.dumps(cases, ensure_ascii=False))
            return 0
        for case in cases:
            print(f"{case['id']}  {case['title'][args.lang]}  ({case['scenario']}, {case['status']})")
        if not cases:
            print("No cases in the active knowledge pack")
        return 0
    case = next((case for case in cases if case["id"] == args.id), None)
    if case is None:
        print(f"Unknown case: {args.id}")
        return 1
    if args.json:
        print(json.dumps(case, ensure_ascii=False))
        return 0
    print(f"{case['id']}  {case['title'][args.lang]}")
    print(f"Scenario: {case['scenario']} ({case['status']})")
    print("Needs: " + ", ".join(case["needs"]))
    for line in case["says"][args.lang]:
        print(f"  • {line}")
    print(f"Result: {case['result'][args.lang]}")
    if "gap" in case:
        print(f"Gap: {case['gap'][args.lang]}")
    if case.get("tutorials"):
        print("Tutorials: " + ", ".join(case["tutorials"]))
    return 0
