"""Generate plaintext email drafts replying to Sashiko reviews.

For the last-sent iteration of the current b4-prepared series, fetch the
per-patch reviews from both Sashiko instances, keep only patches that have at
least one non-pre-existing finding, and emit one reply email per such patch.
Each email quotes (one extra "> " level) the last message of the review's raw
interaction log for every instance that flagged it.

Usage:
    sashiko-drafts [--msgid ID-OR-URL] [--output-dir DIR]
                   [--signature X | --no-signature | --signature-file F]

With --output-dir: write raw RFC-822 .eml files into DIR.
Without it:        APPEND each message to the IMAP "Drafts" mailbox, using the
                   SMTP credentials from `git config sendemail.*`.
"""

from __future__ import annotations

import argparse
import imaplib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, unquote

import httpx

from . import __version__

# (label, base url); list order == section order in the composed body.
INSTANCES = [
    ("non-net", "https://sashiko.dev"),
    ("net", "https://netdev-ai.bots.linux.dev/sashiko"),
]
DRAFTS_MAILBOX = "Drafts"
IMAPS_PORT = 993


@dataclass
class Draft:
    message_id: str  # -> In-Reply-To / References
    subject: str  # API subject (keeps "[PATCH ...]"); -> "Re: <subject>"
    quotes: dict[str, str] = field(default_factory=dict)  # {label: "> "-prefixed review text}


class Patch(NamedTuple):
    subject: str
    count: int  # combined non-pre-existing findings across instances
    part_index: int
    message_id: str
    quotes_todo: list[tuple[str, str, int]]  # [(label, base, review_id)] for Phase 2


# --------------------------------------------------------------------------- git/b4
def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, exiting with a one-liner when it cannot be run or fails."""
    try:
        return subprocess.run(argv, check=check, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(f"error: {argv[0]}: not found in PATH")
    except subprocess.CalledProcessError as e:
        reason = e.stderr.strip() or f"exit status {e.returncode}"
        sys.exit(f"error: `{' '.join(argv)}` failed: {reason}")


def git_config_get(name: str) -> str | None:
    """Value of a git config key; None when unset, "" when set to empty."""
    p = _run(["git", "config", "--get", name], check=False)
    if p.returncode not in (0, 1):
        sys.exit(f"error: `git config --get {name}` failed: {p.stderr.strip()}")
    # `--get` prints the value plus a newline; an empty value is just "\n".
    return p.stdout.rstrip("\n") if p.returncode == 0 else None


def prep_cover() -> str:
    """Cover Message-ID of the last sent iteration from `b4 prep`."""
    info = _run(["b4", "prep", "--show-info"]).stdout
    best_n, cover = -1, None
    for line in info.splitlines():
        m = re.match(r"series-v(\d+):\s*(.+)", line)
        if m and int(m.group(1)) > best_n:
            best_n, cover = int(m.group(1)), m.group(2).split()[-1]
    if cover is None:
        sys.exit("error: no sent iteration found in `b4 prep --show-info`")
    return cover


def subject_slug(subject: str) -> str:
    """Filename slug for an API patch subject, format-patch style.

    Drops any leading "[PATCH ...]"-style bracket prefixes and turns
    non-alphanumeric runs into '-'. (git's own `%f` keeps the bracket
    prefix, hence this custom version.)"""
    subj = re.sub(r"^(\[[^\]]*\]\s*)+", "", subject)
    return re.sub(r"[^A-Za-z0-9_.]+", "-", subj).strip("-")


def parse_msgid(arg: str) -> str:
    """Accept a raw Message-ID or a URL to it (lore.kernel.org or sashiko.dev).

    Returns the cover Message-ID: the URL's last path segment is URL-decoded and
    the b4 part index (-vN-M-) is zeroed so a per-patch Message-ID resolves to
    its patchset.
    """
    s = arg.strip()
    if "://" in s:
        s = s.split("#")[-1].rstrip("/").rsplit("/", 1)[-1]
    s = unquote(s).strip().strip("<>")
    return re.sub(r"-v(\d+)-(\d+)-", r"-v\1-0-", s, count=1)


# -------------------------------------------------------------------- signature
def resolve_signature(args: argparse.Namespace) -> str | None:
    """Signature handling inspired by git format-patch.

    1. `--no-signature` / `--signature=<x>`
    2. `--signature-file=<file>`
    3. `format.signature` git config
    4. `format.signatureFile` git config
    5. default: no signature

    An empty signature (explicit "", empty file, or empty config) suppresses
    the signature block entirely, including the `-- ` separator line.
    """
    if args.no_signature:
        return None
    if args.signature is not None:
        return args.signature
    if args.signature_file is not None:
        return _read_signature_file(args.signature_file)
    cfg = git_config_get("format.signature")
    if cfg is not None:
        return cfg
    cfg_file = git_config_get("format.signatureFile")
    if cfg_file is not None:
        return _read_signature_file(cfg_file, expand=True)
    return None


def _read_signature_file(path: str, *, expand: bool = False) -> str:
    if expand:  # git's git_config_pathname() expands ~ for config-supplied paths
        path = str(Path(path).expanduser())
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        sys.exit(f"error: unable to read signature file '{path}': {e.strerror}")


def _append_signature(body: str, signature: str | None) -> str:
    """Append git's signature block to the body (builtin/log.c print_signature).

    A line with `-- ` separates the body from the signature per RFC 3676, then
    the signature text verbatim, then a final newline.
    """
    if not signature:
        return body
    body += "-- \n" + signature
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n"


# ----------------------------------------------------------------- sashiko HTTP
def get_json(client: httpx.Client, url: str) -> dict:
    r = client.get(url)
    r.raise_for_status()
    return r.json()


def patchset(client: httpx.Client, base: str, cover: str) -> dict:
    """Fetch a patchset, following pagination when a page comes back full.

    The server caps page size (50 at the time of writing) and reports it in the
    response's "limit" field; an exhausted page returns empty lists. New entries
    are deduped by id in case a server ignores the page parameter.
    """
    url = f"{base}/api/patchset?id={quote(cover, safe='')}"
    data = get_json(client, f"{url}&page=1&limit=50")
    cap = data.get("limit", 50)
    seen_p = {p["id"] for p in data["patches"]}
    seen_r = {r["id"] for r in data["reviews"]}
    page = 1
    while len(data["patches"]) >= cap or len(data["reviews"]) >= cap:
        page += 1
        nxt = get_json(client, f"{url}&page={page}&limit={cap}")
        new_p = [p for p in nxt["patches"] if p["id"] not in seen_p]
        new_r = [r for r in nxt["reviews"] if r["id"] not in seen_r]
        if not new_p and not new_r:
            break
        data["patches"] += new_p
        data["reviews"] += new_r
        seen_p.update(p["id"] for p in new_p)
        seen_r.update(r["id"] for r in new_r)
    return data


def quote_text(client: httpx.Client, base: str, review_id: int) -> str:
    """Last assistant message of the review log, one extra quote level."""
    data = get_json(client, f"{base}/api/review?id={review_id}")
    logs = json.loads(data["logs"]) if isinstance(data["logs"], str) else data["logs"]
    text = ""
    for entry in reversed(logs):
        if entry.get("role") not in ("assistant", "model"):
            continue
        text = _entry_text(entry)
        if text:
            break
    text = text.rstrip("\n")
    if not text:
        return ""
    return "\n".join(("> " + ln) if ln else ">" for ln in text.split("\n"))


def _entry_text(entry: dict) -> str:
    parts = []
    if isinstance(entry.get("content"), str) and entry["content"]:
        parts.append(entry["content"])
    for p in entry.get("parts", []):
        if p.get("text") and not p.get("thought"):
            parts.append(p["text"])
    return "".join(parts)


# --------------------------------------------------------------- orchestration
def drafts(cover: str, client: httpx.Client) -> tuple[list[Patch], str, str, dict[str, str]]:
    """Phase 1: fetch patchset metadata for each instance (no review bodies yet).

    Return (patches, to, cc, found). Every Patch (part-index order) carries its
    combined non-pre-existing finding count and the (label, base, review_id)
    triples whose last assistant message Phase 2 still needs to fetch.
    """
    meta: dict[str, tuple[int, str]] = {}  # message_id -> (part_index, subject)
    counts: dict[str, int] = {}  # message_id -> # non-preexisting findings
    todo: dict[str, list[tuple[str, str, int]]] = {}  # message_id -> [(label, base, review_id)]
    found: dict[str, str] = {}
    to = cc = ""
    with ThreadPoolExecutor(max_workers=len(INSTANCES)) as ex:
        pending = [
            (label, base, ex.submit(patchset, client, base, cover)) for label, base in INSTANCES
        ]
        for label, base, fut in pending:
            try:
                ps = fut.result()
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    print(
                        f"warning: {label}: HTTP {e.response.status_code}, skipping instance",
                        file=sys.stderr,
                    )
                continue  # series not present on this instance
            except httpx.RequestError as e:
                print(f"warning: {label} sashiko unreachable: {e}", file=sys.stderr)
                continue
            found[label] = base
            to = to or ps.get("to", "")
            cc = cc or ps.get("cc", "")
            patches_by_id = {p["id"]: p for p in ps["patches"]}
            for p in ps["patches"]:
                meta.setdefault(p["message_id"], (p["part_index"], p["subject"]))
            for r in ps["reviews"]:
                if r.get("status") != "Reviewed":
                    continue
                raw = r.get("output") or {}
                out = json.loads(raw) if isinstance(raw, str) else raw
                nonpre = [f for f in out.get("findings", []) if not f.get("preexisting")]
                if not nonpre:
                    continue
                p = patches_by_id.get(r["patch_id"])
                if p is None:
                    continue  # review of something that is not a patch (e.g. cover letter)
                mid = p["message_id"]
                counts[mid] = counts.get(mid, 0) + len(nonpre)
                todo.setdefault(mid, []).append((label, base, r["id"]))
    patches = [
        Patch(subj, counts.get(mid, 0), pi, mid, todo.get(mid, []))
        for mid, (pi, subj) in sorted(meta.items(), key=lambda kv: kv[1][0])
    ]
    return patches, to, cc, found


def series_url(base: str, cover: str) -> str:
    return f"{base}/#/patchset/{quote(cover, safe='')}"


def message_bytes(
    draft: Draft,
    frm: tuple[str, str],
    to: str,
    cc: str,
    cover: str,
    signature: str | None,
    multi: bool,
) -> bytes:
    sections = []
    for label, base in INSTANCES:
        if label not in draft.quotes:
            continue
        url = series_url(base, cover)
        name = f"{label} sashiko" if multi else "sashiko"
        sections.append(f"Replying to {name}\n{url}\n\n{draft.quotes[label]}")
    body = _append_signature("\n\n---\n\n".join(sections) + "\n\nThanks,\n", signature)

    msg = EmailMessage(policy=SMTP)
    msg["From"] = formataddr(frm)
    if to:
        msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = "Re: " + draft.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["In-Reply-To"] = f"<{draft.message_id}>"
    msg["References"] = f"<{draft.message_id}>"
    msg.set_content(body, cte="quoted-printable")
    return msg.as_bytes()


# ------------------------------------------------------------------------- emit
def emit(items: list[tuple[str, bytes]], output_dir: Path | None) -> None:
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename, data in items:
            (output_dir / filename).write_bytes(data)
        return
    creds = {k: git_config_get(f"sendemail.{k}") for k in ("smtpserver", "smtpuser", "smtppass")}
    missing = [f"sendemail.{k}" for k, v in creds.items() if not v]
    if missing:
        sys.exit(f"error: IMAP mode requires git config {', '.join(missing)} (or use --output-dir)")
    now = imaplib.Time2Internaldate(time.time())
    try:
        with imaplib.IMAP4_SSL(creds["smtpserver"], IMAPS_PORT, timeout=30) as imap:
            imap.login(creds["smtpuser"], creds["smtppass"])
            for i, (filename, data) in enumerate(items):
                typ, resp = imap.append(DRAFTS_MAILBOX, r"(\Draft)", now, data)
                if typ != "OK":
                    sys.exit(
                        f"error: IMAP APPEND failed for {filename} "
                        f"({i}/{len(items)} drafts written): {typ} {resp}"
                    )
    except (OSError, imaplib.IMAP4.error) as e:
        sys.exit(f"error: IMAP {DRAFTS_MAILBOX}: {e}")


# ------------------------------------------------------------------------ main
def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if sys.stderr.isatty() else s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate email drafts replying to Sashiko reviews.")
    ap.add_argument(
        "--msgid",
        help="cover Message-ID or lore.kernel.org link (default: last sent b4 iteration)",
    )
    ap.add_argument(
        "--output-dir", help="write .eml files here instead of IMAP-appending to Drafts"
    )
    ap.add_argument("--signature", help="add a signature (default: none)")
    ap.add_argument("--no-signature", action="store_true", help="do not add a signature")
    ap.add_argument("--signature-file", help="read the signature from a file")
    args = ap.parse_args(argv)

    if args.msgid:
        cover = parse_msgid(args.msgid)
    else:
        cover = prep_cover()
        print(f"found b4 msgid:  {quote(cover, safe='')}\n", file=sys.stderr)

    frm = (git_config_get("user.name"), git_config_get("user.email"))
    if not all(frm):
        sys.exit("error: git config user.name and user.email must be set")
    signature = resolve_signature(args)

    with httpx.Client(
        headers={"User-Agent": f"sashiko-drafts/{__version__}"},
        timeout=httpx.Timeout(60.0),
        follow_redirects=True,
    ) as client:
        patches, to, cc, found = drafts(cover, client)
        if not found:
            print("error: patchset not found on any sashiko instance", file=sys.stderr)
            return 1
        multi = len(found) > 1
        if not to:
            print(
                "warning: no To address reported by any instance; drafts have no recipient",
                file=sys.stderr,
            )
        width = max(len(f"{lab} sashiko:") for lab, _ in INSTANCES)
        for label, base in INSTANCES:
            right = series_url(base, cover) if label in found else "(series not found)"
            print(f"{label} sashiko:".ljust(width) + " " + right, file=sys.stderr)
        print(file=sys.stderr)

        # Phase 2: fetch every review log concurrently, then assemble drafts in
        # part-index order. Fetch and assemble are separate steps so a per-call
        # failure (logged, then skipped) can't desync the patch <-> text mapping.
        quotes: dict[tuple[str, int], str] = {}
        with ThreadPoolExecutor() as ex:
            fut_to_review = {
                ex.submit(quote_text, client, base, rid): (label, rid)
                for p in patches
                for label, base, rid in p.quotes_todo
            }
            for fut in as_completed(fut_to_review):
                label, rid = fut_to_review[fut]
                try:
                    text = fut.result()
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                    print(f"warning: {label}: failed to fetch review {rid}: {e}", file=sys.stderr)
                    continue
                if text:
                    quotes[(label, rid)] = text
                else:
                    print(
                        f"warning: {label}: review {rid} has no assistant message, skipped",
                        file=sys.stderr,
                    )

        items: list[tuple[str, bytes]] = []
        for subject, count, part_index, message_id, quotes_todo in patches:
            patch_quotes: dict[str, str] = {}
            for label, _base, rid in quotes_todo:
                text = quotes.get((label, rid))
                if not text:
                    continue
                if label in patch_quotes:
                    sys.exit(
                        f"error: {label}: multiple reviews for the same patch, "
                        f"cannot pick one: {subject}"
                    )
                patch_quotes[label] = text
            ann = "skipped" if count == 0 else f"{count} new finding{'s' if count != 1 else ''}"
            print(f"{subject} {_bold(f'({ann})')}", file=sys.stderr)
            if count == 0:
                continue
            if not patch_quotes:
                print(f"warning: no usable review text for: {subject}", file=sys.stderr)
                continue
            items.append(
                (
                    f"{part_index:02d}-{subject_slug(subject)}.eml",
                    message_bytes(
                        Draft(message_id, subject, patch_quotes),
                        frm,
                        to,
                        cc,
                        cover,
                        signature,
                        multi,
                    ),
                )
            )
        if not items:
            print("no drafts to write", file=sys.stderr)
            return 0
        output_dir = Path(args.output_dir) if args.output_dir else None
        emit(items, output_dir)

    where = f"{output_dir}" if output_dir else f"IMAP {DRAFTS_MAILBOX}"
    plural = "s" if len(items) > 1 else ""
    print(f"\nwrote {len(items)} draft{plural} to {where}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
