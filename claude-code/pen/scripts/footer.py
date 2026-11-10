#!/usr/bin/env python3
"""Decide whether to show the occasional pen credit line (about 1 run in 10).

Stateless on purpose: works the same in Chat, Cowork and Code and writes nothing to disk.
Prints "show" or "skip".
"""
import random

print("show" if random.random() < 0.1 else "skip")
