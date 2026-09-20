#!/usr/bin/env python3
"""Repair malformed model-generated patches so GNU patch / git apply can consume them.

Fixes applied:
- strip markdown code fences if present
- blank lines inside a hunk body -> " " context lines
- recompute hunk header counts from the actual body
- replace placeholder line numbers (e.g. "XXX") with a monotonic guess
  (GNU patch locates hunks by context search, so an approximate start is fine)
- recompute new-file start from old start + accumulated offset
- drop trailing blank lines at the end of a hunk (usually separators, and
  removing trailing context never prevents a match)
- ensure the patch ends with a newline
"""

import json
import re
import sys

HUNK_RE = re.compile(r"^@@ -(\S+?)(?:,(\S+))? \+(\S+?)(?:,(\S+))? @@(.*)$")
FILE_HEADER_PREFIXES = (
    "diff --git ", "index ", "--- ", "+++ ", "new file mode ",
    "deleted file mode ", "old mode ", "new mode ", "similarity index ",
    "rename from ", "rename to ", "Binary files ",
)


def _to_int(s, default):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def fix_patch(text):
    # strip markdown fences
    text = text.strip("\n")
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    lines = text.split("\n")
    out = []
    i = 0
    # per-file running offset for recomputing new_start
    offset = 0
    prev_old_end = 0

    while i < len(lines):
        line = lines[i]
        m = HUNK_RE.match(line)
        if not m:
            if line.startswith("diff --git ") or line.startswith("--- "):
                offset = 0
                prev_old_end = 0
            out.append(line)
            i += 1
            continue

        # collect hunk body
        i += 1
        body = []
        while i < len(lines):
            l = lines[i]
            if HUNK_RE.match(l) or any(l.startswith(p) for p in FILE_HEADER_PREFIXES):
                break
            if l == "":
                body.append(" ")
            elif l[0] in (" ", "+", "-", "\\"):
                body.append(l)
            else:
                # stray line without prefix: assume it's a context line
                body.append(" " + l)
            i += 1

        # drop trailing blank-context separator lines
        while body and body[-1].strip() == "" and not body[-1].startswith(("+", "-")):
            body.pop()

        old_count = sum(1 for l in body if l[0] in (" ", "-"))
        new_count = sum(1 for l in body if l[0] in (" ", "+"))

        old_start = _to_int(m.group(1), None)
        if old_start is None or old_start <= prev_old_end:
            old_start = prev_old_end + 1
        new_start = old_start + offset

        header = "@@ -%d,%d +%d,%d @@%s" % (
            old_start, old_count, new_start, new_count, m.group(5))
        out.append(header)
        out.extend(body)

        prev_old_end = old_start + old_count - 1
        offset += new_count - old_count

    return "\n".join(out) + "\n"


def main():
    src = "outputs/swebench/predictions_harness.json"
    dst = "outputs/swebench/predictions_harness_fixed.json"
    preds = json.load(open(src))
    changed = 0
    for p in preds:
        fixed = fix_patch(p["model_patch"])
        if fixed != p["model_patch"]:
            changed += 1
        p["model_patch"] = fixed
    json.dump(preds, open(dst, "w"), indent=2)
    print(f"{changed}/{len(preds)} patches modified -> {dst}")


if __name__ == "__main__":
    main()
