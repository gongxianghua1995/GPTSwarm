#!/usr/bin/env python3
"""Runs INSIDE an swebench instance container (cwd=/testbed).

Reads a normalized unified diff from /tmp/p.patch, relocates every hunk
against the real source files, rewrites old-side lines to match the file
exactly, repairs hallucinated context (edges and, via difflib alignment,
interior lines), pads each hunk to 3 lines of real context per edge
(git apply / GNU patch anchor context-less hunk edges to BOF/EOF), and
prints the reconciled patch to stdout. Status per hunk goes to stderr."""

import re
import sys
from difflib import SequenceMatcher

HUNK_RE = re.compile(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@(.*)$")
CTX = 3


def parse(patch_lines):
    """-> list of (header_lines, [(hunk_match, body_lines), ...]) per file."""
    files = []
    i = 0
    n = len(patch_lines)
    while i < n:
        if patch_lines[i].startswith("diff --git ") or (
            patch_lines[i].startswith("--- ") and i + 1 < n and patch_lines[i + 1].startswith("+++ ")
        ):
            header = []
            while i < n and not HUNK_RE.match(patch_lines[i]):
                header.append(patch_lines[i])
                i += 1
            hunks = []
            while i < n:
                m = HUNK_RE.match(patch_lines[i])
                if not m:
                    break
                i += 1
                body = []
                while i < n and patch_lines[i][:1] in (" ", "+", "-", "\\") and not HUNK_RE.match(patch_lines[i]):
                    if patch_lines[i].startswith("--- ") or patch_lines[i].startswith("+++ "):
                        break
                    body.append(patch_lines[i])
                    i += 1
                hunks.append((m, body))
            files.append((header, hunks))
        else:
            i += 1
    return files


def target_path(header):
    for h in header:
        if h.startswith("+++ b/"):
            return h[6:].strip()
        if h.startswith("+++ ") and not h.startswith("+++ /dev/null"):
            return h[4:].strip()
    for h in header:
        if h.startswith("diff --git "):
            parts = h.split()
            if len(parts) >= 4:
                return parts[3][2:] if parts[3].startswith("b/") else parts[3]
    return None


def _search(file_lines, old_lines, hint):
    def candidates(norm):
        nf = [norm(l) for l in file_lines]
        no = [norm(l) for l in old_lines]
        return [s for s in range(len(nf) - len(no) + 1) if nf[s:s + len(no)] == no]

    for norm in (lambda l: l, lambda l: l.rstrip(), lambda l: l.strip()):
        cand = candidates(norm)
        if cand:
            return min(cand, key=lambda s: abs(s - (hint - 1)))
    return None


def _edge_runs(body):
    lead = 0
    for l in body:
        if l[:1] == " ":
            lead += 1
        else:
            break
    trail = 0
    for l in reversed(body):
        if l[:1] == " ":
            trail += 1
        else:
            break
    return lead, trail


def _anchored(lines):
    return len(set(l.strip() for l in lines if l.strip())) >= 2


def exact_relocate(file_lines, body, hint):
    """Exact/normalized contiguous match, trimming up to 3 hallucinated
    context lines per edge. Returns (start0, new_body) or None."""
    lead, trail = _edge_runs(body)
    for f in range(0, 4):
        lt = min(f, lead)
        tt = min(f, trail)
        kept = body[lt:len(body) - tt if tt else len(body)]
        old_lines = [l[1:] for l in kept if l[:1] in (" ", "-")]
        if not old_lines or (f > 0 and not _anchored(old_lines)):
            break
        pos = _search(file_lines, old_lines, hint)
        if pos is None:
            continue
        new_body = []
        k = 0
        for l in kept:
            if l[:1] in (" ", "-"):
                new_body.append(l[:1] + file_lines[pos + k])
                k += 1
            else:
                new_body.append(l)
        return pos, new_body
    return None


def align_relocate(file_lines, body):
    """difflib fallback: align the hunk's old side to the file, tolerating
    hallucinated interior lines. Returns (start0, new_body) or None."""
    old = [(l[:1], l[1:]) for l in body]
    no = [c.strip() for p, c in old if p in (" ", "-")]
    nonblank_total = sum(1 for x in no if x)
    if nonblank_total < 3:
        return None
    nf = [l.strip() for l in file_lines]
    sm = SequenceMatcher(None, no, nf, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks:
        return None
    matched_nonblank = sum(
        1 for b in blocks for k in range(b.size) if no[b.a + k])
    if matched_nonblank < max(3, int(0.5 * nonblank_total)):
        return None
    lo = min(b.b for b in blocks)
    hi = max(b.b + b.size for b in blocks)
    if hi - lo > 3 * len(no) + 20:
        return None
    amap = {}
    for b in blocks:
        for k in range(b.size):
            amap[b.a + k] = b.b + k
    out = []
    oi = 0
    f = lo
    for p, c in old:
        if p == "+":
            out.append("+" + c)
            continue
        if p == "\\":
            continue
        j = amap.get(oi)
        if j is not None and j >= f:
            while f < j:
                out.append(" " + file_lines[f])
                f += 1
            out.append(p + file_lines[j])
            f = j + 1
        # model line absent from file: nothing to delete / no such context
        oi += 1
    while f < hi:
        out.append(" " + file_lines[f])
        f += 1
    if not any(l[:1] in ("+", "-") for l in out):
        return None
    return lo, out


def main():
    patch_text = open("/tmp/p.patch").read()
    lines = patch_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    out = []
    for header, hunks in parse(lines):
        path = target_path(header)
        try:
            file_lines = open("/testbed/" + path).read().split("\n")
            if file_lines and file_lines[-1] == "":
                file_lines.pop()
        except (TypeError, OSError):
            sys.stderr.write("MISSING_FILE %s\n" % path)
            out.extend(header)
            for m, body in hunks:
                out.append(m.group(0))
                out.extend(body)
            continue

        out.extend(header)
        located = []
        for m, body in hunks:
            hint = int(m.group(1))
            res = exact_relocate(file_lines, body, hint)
            how = "OK"
            if res is None:
                res = align_relocate(file_lines, body)
                how = "ALIGNED"
            if res is None:
                sys.stderr.write("NOMATCH %s @@ -%s\n" % (path, m.group(1)))
                located.append((hint, m, body, False))
                continue
            pos, new_body = res
            sys.stderr.write("%s %s %s->%d\n" % (how, path, m.group(1), pos + 1))
            located.append((pos + 1, m, new_body, True))

        located.sort(key=lambda t: t[0])
        # pad every relocated hunk to CTX real context lines per edge
        for idx, (start, m, body, ok) in enumerate(located):
            if not ok:
                continue
            old_count = sum(1 for l in body if l[:1] in (" ", "-"))
            lead, trail = _edge_runs(body)
            if lead >= len(body):
                continue
            prev_end = 0
            if idx > 0:
                ps, _, pb, _ = located[idx - 1]
                prev_end = ps + sum(1 for l in pb if l[:1] in (" ", "-")) - 1
            next_start = len(file_lines) + 1
            if idx + 1 < len(located):
                next_start = located[idx + 1][0]
            add_lead = min(CTX - lead, start - 1 - prev_end) if lead < CTX else 0
            hunk_end = start + old_count - 1
            add_trail = min(CTX - trail, len(file_lines) - hunk_end,
                            next_start - 1 - hunk_end) if trail < CTX else 0
            if add_lead > 0:
                body = [" " + file_lines[j - 1]
                        for j in range(start - add_lead, start)] + body
                start -= add_lead
            if add_trail > 0:
                body = body + [" " + file_lines[j - 1]
                               for j in range(hunk_end + 1, hunk_end + 1 + add_trail)]
            located[idx] = (start, m, body, ok)

        offset = 0
        for start, m, body, ok in located:
            old_count = sum(1 for l in body if l[:1] in (" ", "-"))
            new_count = sum(1 for l in body if l[:1] in (" ", "+"))
            out.append("@@ -%d,%d +%d,%d @@%s" % (
                start, old_count, start + offset, new_count, m.group(5)))
            out.extend(body)
            offset += new_count - old_count

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
