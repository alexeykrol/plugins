#!/usr/bin/env python3
"""Count text length for X and LinkedIn.

Usage: python3 count.py FILE [--blocks]
  FILE      text file (use - for stdin)
  --blocks  treat blank-line-separated blocks as thread posts and report each

X length follows the twitter-text weighting: URLs count as 23, most Latin,
Cyrillic and common punctuation count as 1, other characters (CJK, emoji) as 2.
LinkedIn length is the plain character count, including spaces and newlines.
"""
import re
import sys

URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
LIGHT = [(0, 4351), (8192, 8205), (8208, 8223), (8242, 8247)]


def char_weight(ch):
    cp = ord(ch)
    return 1 if any(a <= cp <= b for a, b in LIGHT) else 2


def x_length(text):
    total = 0
    pos = 0
    for m in URL_RE.finditer(text):
        total += sum(char_weight(c) for c in text[pos:m.start()]) + 23
        pos = m.end()
    total += sum(char_weight(c) for c in text[pos:])
    # zero-width joiners and variation selectors inside emoji sequences
    total -= 2 * sum(1 for c in text if c in "‍️")
    return total


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    blocks = "--blocks" in sys.argv
    src = sys.stdin if not args or args[0] == "-" else open(args[0], encoding="utf-8")
    text = src.read().strip("\n")
    print(f"chars (LinkedIn): {len(text)}")
    print(f"x-weighted: {x_length(text)}")
    if blocks:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        over = 0
        for i, p in enumerate(parts, 1):
            n = x_length(p)
            flag = "  OVER 280" if n > 280 else ""
            over += n > 280
            print(f"block {i}: {n}{flag}")
        print(f"blocks: {len(parts)}, over limit: {over}")


if __name__ == "__main__":
    main()
