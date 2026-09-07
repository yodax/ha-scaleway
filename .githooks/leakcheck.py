#!/usr/bin/env python3
"""Shared leak-scanning logic for the pre-commit and commit-msg hooks.

This repository is public and is developed against a real Scaleway account. Two
different kinds of thing must never reach a commit:

- **Live credentials.** A Scaleway API key is an access key plus a secret key
  with real spend attached to it, and this integration is written and tested by
  running it against a real one. That is materially worse than a leaked address:
  a published secret key is an open door to someone's cloud account until it is
  revoked.
- **Homelab and account specifics** — LAN addresses, the hypervisor hostname and
  SSH alias, container mount paths, the organization id. This repo gitignores
  CLAUDE.md and .claude/ precisely so that material stays out, but a gitignore
  only protects the files it names; it does nothing about the same strings being
  pasted into a source file, a README, or a commit message.

GitHub keeps deleted content reachable by commit SHA even after a force-push, so
"notice it later and remove it" is not a recovery path. Hence a preventive gate.

── Five lessons are baked into the shape of this file ────────────────────────

The first four were learned the hard way on the sibling ha-trappers repo, where
this gate originated. They are reproduced rather than rediscovered.

1. WHY PYTHON, NOT A grep PIPELINE. The first version filtered added lines with
   `grep -E '^\\+' | grep -v '^\\+\\+\\+'`. On this machine `grep` is ugrep, which
   rejects `^\\+\\+\\+` ("invalid syntax") — the pipeline errored, `|| true`
   swallowed it, and the hook exited 0 on a staged diff containing a live
   credential. It was installed, looked right, and guarded nothing. Python's
   `re` has one dialect everywhere, and every subprocess failure below aborts
   the commit rather than passing it.

2. WHY THE SENSITIVE PATTERNS ARE NOT IN THIS FILE. The second version listed the
   real identifiers inline as patterns — and was correctly blocked by itself on
   the very first commit. A public repo cannot carry the list of strings it is
   guarding; the guard would be the leak. So this file holds only patterns that
   are generic infrastructure vocabulary or match on *shape* (see SCALEWAY
   below), and loads the identity-specific ones from a file outside the repo.

3. WHY .githooks/ IS EXEMPT FROM THE GENERIC PATTERNS. v3 then blocked its own
   test fixtures — test-pre-commit.sh has to contain a sample LAN address in
   order to assert that LAN addresses are blocked. The exemption is scoped to
   .githooks/ and to the generic half only: identity patterns still apply there,
   because .githooks/ is exactly where lesson 2's mistake would land.

4. WHY COMMIT MESSAGES ARE SCANNED TOO. v4 checked only the staged diff. An agent
   working on the sibling repo redacted a real account figure from the tree and
   from a published release note, and then described the same figure in the
   commit message that did the redacting — which the gate never looked at, and
   which cannot be recalled once pushed. A gate that covers the files but not the
   prose about the files is a gate with a door next to it.

5. WHY THE CREDENTIAL PATTERNS TOLERATE PLACEHOLDERS. This repo's test suite has
   to contain things shaped like Scaleway credentials in order to test the code
   that consumes them. A pattern that blocked every credential-shaped string
   would block `tests/` on every commit, and a gate that cries wolf gets disabled
   or routed around with SKIP_LEAK_CHECK, which is worse than no gate. So the
   patterns below match the shape of a *real* key and deliberately do not match
   an obvious placeholder — an access key with no digits in it, or a secret whose
   first block is one repeated character. Write fixtures that way. The rule is
   in FIX_ADVICE so it is in front of whoever trips the gate.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Generic patterns. These are ordinary infrastructure vocabulary — publishing them
# reveals nothing about any particular person or network, so they live in the repo.
GENERIC_EXEMPT_PREFIX = ".githooks/"

# Scaleway credentials. These match on SHAPE, not on any particular key, so they
# are safe to publish — and unlike the identity patterns they must work for an
# outside contributor too, since anyone developing this integration is doing it
# against their own live account.
#
# Both deliberately exclude the obvious placeholder forms; see lesson 5.
SCALEWAY = [
    (
        # Access key: literal "SCW" + 17 uppercase alphanumerics.
        #
        # The only exemption is a run of ONE repeated character
        # (SCWXXXXXXXXXXXXXXXXX, SCWYYYYYYYYYYYYYYYYY), which no real key can
        # be. An earlier version exempted any key with no digits in it, on the
        # assumption that real keys always contain digits — an assumption drawn
        # from a sample of one and not established anywhere. That exemption was
        # far wider than the placeholders needed it to be.
        r"\bSCW(?!([A-Z0-9])\1{16}\b)[A-Z0-9]{17}\b",
        "Scaleway access key (in fixtures use SCW + 17 of the same letter)",
    ),
    (
        # Secret key: a UUID that is not one of the placeholder forms, ANYWHERE.
        #
        # The first version required a nearby label (secret_key, X-Auth-Token)
        # within a few characters. That is a fail-open design and it leaked:
        # `secret_key =        "<uuid>"` slipped through on whitespace alone,
        # and a YAML block value, a dict spanning two lines, or a UUID whose
        # label sits on an *unchanged* line the diff never shows would all
        # escape too. A line-based scanner cannot reliably associate a value
        # with a label somewhere else, so it must not try.
        #
        # So: match the value, not the context. The cost is that every UUID has
        # to be written in placeholder form — first block a single repeated
        # character — which the whole test suite already does. That is a real
        # constraint on fixtures, and it is the right way round: a false
        # positive costs a rename, a false negative costs a live credential.
        r"\b(?!([0-9a-fA-F])\1{7}-)"
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "UUID that is not a placeholder (a Scaleway secret key is a UUID)",
    ),
]

GENERIC = [
    (r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "private/loopback IP address"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "private LAN address"),
    (r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "private LAN address"),
    (r"pct\s+(?:exec|push|enter)", "Proxmox container command"),
    (r"\bproxmox\b", "hypervisor name"),
    (r"ssh\s+pve\b", "homelab SSH alias"),
    (r"/tank/docker", "homelab storage path"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", "JWT (a real session token)"),
    (r"\bNL\d{2}[A-Z]{4}\d{10}\b", "IBAN"),
]

FIX_ADVICE = (
    "\n  Fix: replace with a placeholder. For Scaleway credentials specifically,\n"
    "  the gate accepts fixture-shaped values and blocks real-shaped ones:\n"
    "    access key -> SCWXXXXXXXXXXXXXXXXX  (SCW + 17 of the same letter)\n"
    "    secret key -> 00000000-0000-4000-8000-000000000000  (and ANY other\n"
    "                  UUID: first block must be one repeated character)\n"
    "  If it is a real credential that has already been staged, REVOKE IT in the\n"
    "  Scaleway console rather than only unstaging it.\n"
    "  Deploy tooling and account specifics belong in .claude/ or CLAUDE.md, both\n"
    "  of which this repo gitignores. Override only deliberately:\n"
    "    SKIP_LEAK_CHECK=1 git commit ...\n\n"
)


def pattern_file_path() -> str:
    return os.environ.get(
        "SCALEWAY_LEAK_PATTERNS",
        os.path.join(os.path.expanduser("~"), ".config", "ha-scaleway", "leak-patterns.txt"),
    )


def load_private(path: str) -> tuple[list[tuple[str, str]], bool]:
    """Load identity patterns from outside the repo. Returns (patterns, loaded).

    Aborts on a corrupt file rather than checking with fewer patterns, and — see
    below — aborts on a file that exists but defines nothing, which is
    indistinguishable from a mistake and would otherwise mean silently running
    with no identity coverage while every message claimed the private half was
    loaded.
    """
    if not os.path.exists(path):
        return [], False

    private: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            rx, _, desc = line.partition("\t")
            rx = rx.strip()
            if not rx:
                continue
            try:
                re.compile(rx)
            except re.error as exc:
                sys.stderr.write(
                    "leak check: %s line %d is not a valid regex (%s).\n"
                    "Refusing the commit rather than checking with a broken pattern "
                    "list.\n" % (path, lineno, exc)
                )
                sys.exit(1)
            private.append((rx, desc.strip() or "private pattern"))

    if not private:
        sys.stderr.write(
            "leak check: %s exists but defines no patterns.\n"
            "Refusing the commit: an empty identity list is indistinguishable from a\n"
            "mistake, and running with none of them while reporting the private half as\n"
            "loaded is exactly the fail-open this gate exists to prevent.\n"
            "Delete the file to run generic-only deliberately.\n" % path
        )
        sys.exit(1)

    return private, True


def run(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(
            "leak check: could not run (%s exited %d):\n%s\n"
            "Refusing the commit rather than passing unchecked.\n"
            % (" ".join(args), p.returncode, p.stderr.strip())
        )
        sys.exit(1)
    return p.stdout


def added_lines_from_staged_diff() -> list[tuple[str, str]]:
    """Every added line in the staged diff, as (path, text).

    The hunk-state machine is load-bearing. A previous version treated any line
    starting with `+++ ` as the diff's file header — but a *content* line reading
    `++ something` is rendered as `+++ something` in the diff, so an identity
    canary on such a line was silently skipped as metadata. `+++ ` is only a
    header before the first `@@` hunk marker of a file; after it, every `+` line
    is content.
    """
    # -z: NUL-delimited and unquoted. With the default `core.quotePath`, plain
    # --name-only renders "café.txt" as the literal C-escaped string
    # "caf\303\251.txt"; feeding that back as a pathspec matches no file, and an
    # unmatched pathspec yields an empty diff rather than an error — so the file
    # would be committed unscanned.
    staged = [
        f
        for f in run(
            ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]
        ).split("\0")
        if f
    ]
    if not staged:
        return []

    # -U0: only changed lines. Renames and mode changes produce no '+' lines at
    # all, which is correct — they introduce no new content that could leak.
    diff = run(["git", "diff", "--cached", "-U0", "--"] + staged)

    current_file: str | None = None
    in_hunk = False
    added: list[tuple[str, str]] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk and line.startswith("+++ "):
            path = line[4:].strip()
            current_file = path[2:] if path.startswith("b/") else path
            continue
        if in_hunk and line.startswith("+"):
            added.append((current_file or "<unknown>", line[1:]))
    return added


def scan(
    entries: list[tuple[str, str]],
    private: list[tuple[str, str]],
    *,
    apply_generic_exemption: bool,
) -> bool:
    """Report any match. Returns True if something was blocked."""
    failed = False
    # SCALEWAY patterns are grouped with the identity patterns, not the generic
    # ones: they are NOT exempt inside .githooks/. The generic exemption exists
    # so a fixture can contain a sample LAN address; a credential-shaped string
    # that this gate would block is not something a fixture needs, because the
    # patterns already accept the placeholder forms.
    for pattern, why, generic in (
        [(p, w, True) for p, w in GENERIC]
        + [(p, w, False) for p, w in SCALEWAY]
        + [(p, w, False) for p, w in private]
    ):
        rx = re.compile(pattern, re.IGNORECASE)
        scope = [
            (f, t)
            for f, t in entries
            if not (
                generic
                and apply_generic_exemption
                and f.startswith(GENERIC_EXEMPT_PREFIX)
            )
        ]
        hits = [(f, t) for f, t in scope if rx.search(t)]
        if not hits:
            continue
        if not failed:
            sys.stderr.write(
                "\nleak check: BLOCKED — this commit contains content that must not be "
                "published.\n\n"
            )
            failed = True
        sys.stderr.write("  %s:\n" % why)
        for f, t in hits[:5]:
            sys.stderr.write("    %s: %s\n" % (f, t.strip()[:120]))
        if len(hits) > 5:
            sys.stderr.write("    ... and %d more\n" % (len(hits) - 5))
    return failed


def warn_if_generic_only(loaded_private: bool, path: str) -> None:
    """Say plainly which half ran.

    A contributor cloning this repo has no identity-specific pattern file and no
    secrets of the maintainer's to leak, so generic-only is right for them — but
    it must never look like full coverage to the maintainer, whose file has
    simply gone missing.
    """
    if not loaded_private:
        sys.stderr.write(
            "leak check: generic patterns only — no identity pattern file at %s.\n"
            "  (Expected for an outside contributor. If you are the maintainer, restore it:\n"
            "   the identity-specific patterns are deliberately not stored in this repo.)\n"
            % path
        )


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "pre-commit"
    path = pattern_file_path()
    private, loaded = load_private(path)

    if mode == "commit-msg":
        message_file = argv[2]
        with open(message_file, encoding="utf-8", errors="replace") as fh:
            # EVERY line, '#' ones included. An earlier version skipped them on
            # the assumption that git strips comments — but `git commit -m` and
            # `-F` use `cleanup=whitespace` by default, which does not, so
            # `# ZZQQ-CANARY-4711` sailed straight through the gate and into the
            # published message. What gets committed is what gets scanned.
            entries = [("commit message", line.rstrip("\n")) for line in fh]
        # No path, so no .githooks/ exemption applies: a commit message has no
        # legitimate reason to contain a sample LAN address.
        failed = scan(entries, private, apply_generic_exemption=False)
    else:
        failed = scan(
            added_lines_from_staged_diff(), private, apply_generic_exemption=True
        )

    if failed:
        sys.stderr.write(FIX_ADVICE)
        return 1

    warn_if_generic_only(loaded, path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
