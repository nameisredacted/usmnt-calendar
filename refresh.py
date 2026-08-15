#!/usr/bin/env python3
"""Build usmnt-all-levels.ics -- every U.S. men's national team from U-17 up.

    python3 refresh.py              # fetch live, write, print changelog
    python3 refresh.py --offline    # rebuild from cache/ or fixtures/ (no network)
    python3 refresh.py --dry-run    # show the changelog, write nothing

The point of this script is not that it produces a calendar; it is that it
refuses to produce a *confident* calendar out of unconfirmed data. See the
"correctness rules" section of README.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import sources
from sources import (
    CONCACAF_COMPETITIONS_API,
    FIFA_U17_URL,
    TEAM_KEYS,
    TEAM_LABELS,
    USSOCCER_CONTESTANT_IDS,
    EmptyResultError,
    Fixture,
    Kickoff,
    ParserError,
    SourceResult,
    VenueBook,
    Window,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(HERE, "usmnt-all-levels.ics")

# fixtures/ is a FROZEN test corpus: hand-curated, committed, and never
# written by a live run. cache/ is the last good response from each source,
# rewritten every run and gitignored.
#
# These were one directory until a live run was observed rewriting the corpus
# the tests assert against -- which meant the suite silently validated
# whatever the sources happened to return last, and a source changing shape
# would have moved the goalposts instead of failing the build.
FIXTURE_DIR = os.path.join(HERE, "fixtures")
CACHE_DIR = os.path.join(HERE, "cache")
VENUES_PATH = os.path.join(HERE, "venues.json")
SEED_PATH = os.path.join(HERE, "seed.json")

PRODID = "-//usmnt-calendar//US Men's National Teams U17+//EN"
CALNAME = "USMNT (all levels)"

# Assumed on-screen duration of a match, used only to give timed events a
# DTEND. 90 minutes plus half-time plus stoppage. This is the one duration we
# assume, it is stated here and in the README, and it never affects a UID or a
# start time.
MATCH_DURATION = timedelta(hours=2)

# If a run would drop more than this fraction of the previous calendar, stop
# and make a human look. A source outage should not quietly empty someone's
# subscription.
SHRINK_ABORT_RATIO = 0.5

# Custom property carrying a hash of the event's user-visible content. Lets a
# later run tell "this event changed" from "this event is byte-identical" so
# DTSTAMP/SEQUENCE stay put when nothing actually moved.
X_CONTENT_HASH = "X-USMNT-CONTENT-HASH"


# ==========================================================================
# merged events
# ==========================================================================


@dataclass
class Event:
    """A calendar event, after merging every source's claims about one match."""

    uid: str
    summary: str
    team: str
    local_date: date
    competition: str
    venue: Optional[str] = None
    # Exactly one of these three shapes applies:
    kickoff_utc: Optional[datetime] = None            # confirmed single time
    kickoff_span: Optional[Tuple[datetime, datetime]] = None  # sources disagree
    all_day_end: Optional[date] = None                # multi-day placeholder
    description_lines: List[str] = field(default_factory=list)
    sources: Tuple[str, ...] = ()
    unconfirmed: bool = False
    is_window: bool = False

    @property
    def is_timed(self) -> bool:
        return self.kickoff_utc is not None or self.kickoff_span is not None

    def content_hash(self) -> str:
        """Hash of everything a subscriber would see."""
        parts = [
            self.summary,
            self.local_date.isoformat(),
            self.competition,
            self.venue or "",
            self.kickoff_utc.isoformat() if self.kickoff_utc else "",
            "{}..{}".format(*[d.isoformat() for d in self.kickoff_span])
            if self.kickoff_span
            else "",
            self.all_day_end.isoformat() if self.all_day_end else "",
            "\n".join(self.description_lines),
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


# ==========================================================================
# collection
# ==========================================================================


@dataclass
class Collection:
    results: List[SourceResult] = field(default_factory=list)
    failures: List[Tuple[str, str]] = field(default_factory=list)  # (source, message)

    def add(self, name: str, fn) -> None:
        """Run one source in isolation.

        A parser blowing up is recorded and the run continues; that is the
        whole reason parsers are separate functions. Whether the run is still
        publishable is decided later, in :func:`guard`.
        """
        try:
            self.results.append(fn())
        except ParserError as exc:
            self.failures.append((name, str(exc)))
        except Exception as exc:  # noqa: BLE001 - never let one source abort the run
            self.failures.append((name, "unexpected {}: {}".format(type(exc).__name__, exc)))

    @property
    def fixtures(self) -> List[Fixture]:
        return [f for r in self.results for f in r.fixtures]

    @property
    def windows(self) -> List[Window]:
        return [w for r in self.results for w in r.windows]

    @property
    def quarantine(self) -> List[Fixture]:
        return [f for r in self.results for f in r.quarantine]


def collect(*, offline: bool, venues: VenueBook, horizon_days: int = 540) -> Collection:
    col = Collection()
    horizon = date.today() + timedelta(days=horizon_days)

    # --- ussoccer.com: every level -------------------------------------
    for team in TEAM_KEYS:
        url = sources.ussoccer_match_url(USSOCCER_CONTESTANT_IDS[team])
        name = "ussoccer[{}]".format(team)

        def run(team=team, url=url):
            if offline:
                payload = _read_fixture("ussoccer_{}.json".format(team))
            else:
                payload = sources.fetch(url, accept="application/json")
                _save_cache("ussoccer_{}.json".format(team), payload)
            return sources.parse_ussoccer(payload, venues, team=team, source_url=url)

        col.add(name, run)

    # --- concacaf.com: competition windows ------------------------------
    def run_concacaf():
        if offline:
            payload = _read_fixture("concacaf_competitions.json")
        else:
            payload = _fetch_concacaf_all()
            _save_cache("concacaf_competitions.json", payload)
        return sources.parse_concacaf_windows(
            payload, today=date.today(), horizon=horizon
        )

    col.add("concacaf", run_concacaf)

    # --- fifa.com: U-17 World Cup ---------------------------------------
    def run_fifa():
        if offline:
            payload = _read_fixture("fifa_u17wc_qatar2026.html")
        else:
            payload = sources.fetch(FIFA_U17_URL, accept="text/html")
            _save_cache("fifa_u17wc_qatar2026.html", payload)
        return sources.parse_fifa_u17(payload, venues)

    col.add("fifa", run_fifa)

    # --- seed.json: hand-entered ----------------------------------------
    # Registered only when it is switched on and actually has entries. An
    # empty seed file is a legitimate state ("nothing hand-entered"), unlike
    # an empty scrape, so it should not show up as a source failure.
    if _seed_is_active():
        def run_seed():
            with open(SEED_PATH, "rb") as fh:
                payload = fh.read()
            return sources.parse_seed(payload, venues, path=SEED_PATH)

        col.add("seed", run_seed)
    return col


def _seed_is_active() -> bool:
    if not os.path.exists(SEED_PATH):
        return False
    try:
        with open(SEED_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return True  # malformed: let the parser report why
    return bool(data.get("enabled")) and bool(data.get("fixtures"))


def _fetch_concacaf_all() -> bytes:
    """Page through Concacaf's competition list into one JSON array."""
    items: List[dict] = []
    url: Optional[str] = CONCACAF_COMPETITIONS_API
    while url and len(items) < 500:
        raw = sources.fetch(url, accept="application/json", referer="https://www.concacaf.com/")
        data = json.loads(raw.decode("utf-8", "replace"))
        items.extend(data.get("items", []))
        url = (data.get("pagination") or {}).get("nextUrl")
    return json.dumps({"items": items}).encode("utf-8")


def _read_fixture(name: str) -> bytes:
    """Read a saved payload for --offline: freshest cache first, corpus second."""
    for directory in (CACHE_DIR, FIXTURE_DIR):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()
    raise sources.FetchError(
        "no saved payload for {} in {} or {}".format(name, CACHE_DIR, FIXTURE_DIR)
    )


def _save_cache(name: str, payload: bytes) -> None:
    """Keep the last good response so --offline can rebuild without network.

    Writes to cache/ only. Never touches fixtures/ -- overwriting the corpus
    from a live run would let a source change what the tests assert.
    """
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _atomic_write(os.path.join(CACHE_DIR, name), payload)
    except OSError:
        pass  # caching is a convenience, never a reason to fail a run


# ==========================================================================
# merge
# ==========================================================================


def merge(fixtures: Sequence[Fixture], windows: Sequence[Window]) -> List[Event]:
    """Fold every source's fixtures into one event per UID.

    When two sources give the same match two different kickoff times we do not
    arbitrate. The event becomes a block spanning both claims and the
    DESCRIPTION names each source and its time, so the person reading the
    calendar can see there is an open question instead of trusting a coin flip.
    """
    by_uid: "OrderedDict[str, List[Fixture]]" = OrderedDict()
    for f in fixtures:
        uid = f.uid
        if uid is None:
            continue  # already quarantined upstream
        by_uid.setdefault(uid, []).append(f)

    events: List[Event] = []
    for uid, group in by_uid.items():
        events.append(_merge_group(uid, group))
    for w in windows:
        events.append(_window_event(w))

    events.sort(key=lambda e: (e.local_date, e.uid))
    return events


def _merge_group(uid: str, group: List[Fixture]) -> Event:
    primary = group[0]
    src_names = tuple(sorted({f.source for f in group}))

    kickoffs = [f.kickoff for f in group if f.kickoff is not None]
    distinct = sorted({k.utc for k in kickoffs})

    desc: List[str] = []
    kickoff_utc: Optional[datetime] = None
    span: Optional[Tuple[datetime, datetime]] = None
    unconfirmed = False

    if not kickoffs:
        unconfirmed = True
        desc.append("KICKOFF TIME UNCONFIRMED -- shown as an all-day event.")
        desc.append(
            "No source published a confirmed kickoff time. The date below is "
            "confirmed; the time is not. This event will become a timed event "
            "automatically once a time is published."
        )
    elif len(distinct) == 1:
        kickoff_utc = distinct[0]
    else:
        # Genuine disagreement. Span it, name it, do not resolve it.
        span = (distinct[0], distinct[-1])
        unconfirmed = True
        desc.append(
            "CONFLICTING KICKOFF TIMES -- this block spans every reported time "
            "and is NOT a single confirmed kickoff."
        )
        for k in sorted(kickoffs, key=lambda x: (x.utc, x.source)):
            desc.append(
                "  {} reports {}".format(k.source, k.utc.strftime("%Y-%m-%d %H:%M UTC"))
            )
        delta = distinct[-1] - distinct[0]
        desc.append(
            "  Sources disagree by {}. No source has been preferred.".format(
                _humanize(delta)
            )
        )

    if kickoff_utc is not None:
        desc.append("Kickoff {}.".format(kickoff_utc.strftime("%Y-%m-%d %H:%M UTC")))
        if primary.venue_tz:
            local = kickoff_utc.astimezone(sources.ZoneInfo(primary.venue_tz))
            desc.append(
                "Local time at venue: {} ({}, UTC{}).".format(
                    local.strftime("%Y-%m-%d %H:%M"),
                    primary.venue_tz,
                    _offset_str(local.utcoffset()),
                )
            )

    desc.append("Competition: {}".format(primary.competition or "unspecified"))
    if primary.venue:
        desc.append("Venue: {}".format(primary.venue))
    bcast = sorted({b for f in group for b in f.broadcasters})
    if bcast:
        desc.append("TV/stream: {}".format(", ".join(bcast)))

    for f in group:
        for note in f.notes:
            desc.append("[{}] {}".format(f.source, note))

    desc.append("Sources: {}".format(", ".join(src_names)))
    for f in group:
        if f.source_url:
            desc.append("  {} -> {}".format(f.source, f.source_url))

    return Event(
        uid=uid,
        summary=primary.title,
        team=primary.team,
        local_date=primary.local_date,
        competition=primary.competition,
        venue=primary.venue,
        kickoff_utc=kickoff_utc,
        kickoff_span=span,
        description_lines=desc,
        sources=src_names,
        unconfirmed=unconfirmed,
    )


def _window_event(w: Window) -> Event:
    desc = [
        "COMPETITION WINDOW -- no fixtures published yet.",
        "This is a placeholder covering the whole announced window, not a match.",
        "Dates: {} to {} inclusive.".format(w.start.isoformat(), w.end.isoformat()),
    ]
    if w.venues_text:
        desc.append("Venues: {}".format(w.venues_text))
    desc.extend(w.notes)
    desc.append("Source: {} -> {}".format(w.source, w.source_url))
    return Event(
        uid=w.uid,
        summary="[TBD] {}".format(w.name),
        team="window",
        local_date=w.start,
        competition=w.name,
        all_day_end=w.end,
        description_lines=desc,
        sources=(w.source,),
        unconfirmed=True,
        is_window=True,
    )


def _humanize(delta: timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    h, m = divmod(mins, 60)
    if h and m:
        return "{}h {}m".format(h, m)
    return "{}h".format(h) if h else "{}m".format(m)


def _offset_str(off: Optional[timedelta]) -> str:
    if off is None:
        return "?"
    total = int(off.total_seconds() // 60)
    sign = "-" if total < 0 else "+"
    h, m = divmod(abs(total), 60)
    return "{}{}".format(sign, h) if m == 0 else "{}{}:{:02d}".format(sign, h, m)


# ==========================================================================
# ICS output
# ==========================================================================


def build_ics(events: Sequence[Event], previous: Dict[str, dict], now: datetime) -> str:
    lines: List[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:{}".format(PRODID),
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:{}".format(CALNAME),
        "X-WR-CALDESC:{}".format(
            _esc(
                "US men's national teams, U-17 and above. Unconfirmed kickoffs "
                "appear as all-day events; see each event's description."
            )
        ),
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    for ev in events:
        lines.extend(_vevent(ev, previous, now))
    lines.append("END:VCALENDAR")
    return "".join(_fold(line) + "\r\n" for line in lines)


def _vevent(ev: Event, previous: Dict[str, dict], now: datetime) -> List[str]:
    chash = ev.content_hash()
    prev = previous.get(ev.uid)
    # Only stamp a new DTSTAMP/SEQUENCE when the event actually changed --
    # otherwise an unchanged calendar would rewrite every event on every run
    # and look like churn to clients.
    if prev and prev.get(X_CONTENT_HASH) == chash:
        dtstamp = prev.get("DTSTAMP") or _utc_stamp(now)
        sequence = int(prev.get("SEQUENCE") or 0)
    else:
        dtstamp = _utc_stamp(now)
        sequence = (int(prev.get("SEQUENCE") or 0) + 1) if prev else 0

    out = ["BEGIN:VEVENT", "UID:{}".format(ev.uid), "DTSTAMP:{}".format(dtstamp)]
    out.append("SEQUENCE:{}".format(sequence))

    if ev.kickoff_span is not None:
        start, end = ev.kickoff_span
        out.append("DTSTART:{}".format(_utc_stamp(start)))
        out.append("DTEND:{}".format(_utc_stamp(end + MATCH_DURATION)))
    elif ev.kickoff_utc is not None:
        out.append("DTSTART:{}".format(_utc_stamp(ev.kickoff_utc)))
        out.append("DTEND:{}".format(_utc_stamp(ev.kickoff_utc + MATCH_DURATION)))
    else:
        # All-day. DTEND is exclusive per RFC 5545, hence the +1 day.
        end_day = (ev.all_day_end or ev.local_date) + timedelta(days=1)
        out.append("DTSTART;VALUE=DATE:{}".format(ev.local_date.strftime("%Y%m%d")))
        out.append("DTEND;VALUE=DATE:{}".format(end_day.strftime("%Y%m%d")))

    summary = ev.summary
    if ev.kickoff_span is not None:
        summary = "[TIME DISPUTED] " + summary
    elif ev.unconfirmed and not ev.is_window:
        summary = "[TIME TBD] " + summary
    out.append("SUMMARY:{}".format(_esc(summary)))
    out.append("DESCRIPTION:{}".format(_esc("\n".join(ev.description_lines))))
    if ev.venue:
        out.append("LOCATION:{}".format(_esc(ev.venue)))
    out.append("CATEGORIES:{}".format(_esc(TEAM_LABELS.get(ev.team, ev.team.upper()))))
    out.append("STATUS:{}".format("TENTATIVE" if ev.unconfirmed else "CONFIRMED"))
    out.append("TRANSP:TRANSPARENT" if ev.is_window else "TRANSP:OPAQUE")
    out.append("{}:{}".format(X_CONTENT_HASH, chash))
    out.append("END:VEVENT")
    return out


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold to 75 octets per RFC 5545, without splitting a UTF-8 character.

    Continuation lines are emitted as ``CRLF + single space + payload``. The
    space is the fold marker and an unfolder strips exactly it, so a payload
    chunk that itself begins with a space survives the round trip.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    chunks: List[bytes] = []
    start, limit = 0, 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        if end < len(raw):
            # Never cut mid-character: back off over UTF-8 continuation bytes.
            while end > start and (raw[end] & 0xC0) == 0x80:
                end -= 1
            if end == start:  # a single character wider than the budget
                end = min(start + limit, len(raw))
        chunks.append(raw[start:end])
        start = end
        limit = 74  # continuation lines spend one octet on the leading space

    out = chunks[0].decode("utf-8")
    for chunk in chunks[1:]:
        out += "\r\n " + chunk.decode("utf-8")
    return out


# ==========================================================================
# reading the previous file
# ==========================================================================


def read_previous(path: str) -> Dict[str, dict]:
    """Parse the existing .ics into ``{uid: {prop: value}}``.

    Only needs to be good enough to diff against and to carry DTSTAMP and
    SEQUENCE forward; a parse failure is not fatal, it just means the next
    changelog reports everything as added.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return read_previous_text(fh.read())
    except OSError:
        return {}


def read_previous_text(text: str) -> Dict[str, dict]:
    """The parsing half of :func:`read_previous`, over an in-memory calendar."""
    unfolded: List[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw)

    out: Dict[str, dict] = {}
    cur: Optional[dict] = None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur and cur.get("UID"):
                out[cur["UID"]] = cur
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        name, value = line.split(":", 1)
        cur[name.split(";", 1)[0]] = value
        if ";" in name:
            cur["_params_" + name.split(";", 1)[0]] = name.split(";", 1)[1]
    return out


# ==========================================================================
# diff / changelog
# ==========================================================================


@dataclass
class Changelog:
    added: List[Event] = field(default_factory=list)
    changed: List[Tuple[Event, List[str]]] = field(default_factory=list)
    removed: List[Tuple[str, dict]] = field(default_factory=list)
    unconfirmed: List[Event] = field(default_factory=list)
    rescheduled: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed)


def diff(events: Sequence[Event], previous: Dict[str, dict]) -> Changelog:
    log = Changelog()
    new_uids = {e.uid for e in events}

    for ev in events:
        prev = previous.get(ev.uid)
        if prev is None:
            log.added.append(ev)
        else:
            deltas = _field_deltas(ev, prev)
            if deltas:
                log.changed.append((ev, deltas))
        if ev.unconfirmed:
            log.unconfirmed.append(ev)

    for uid, prev in previous.items():
        if uid not in new_uids:
            log.removed.append((uid, prev))

    # A UID contains the date, so a postponed match looks like remove+add
    # rather than a change. Spot the pair and say so out loud, because for a
    # subscriber it means a stale event stays on the calendar.
    def key(uid: str) -> Optional[Tuple[str, str]]:
        bits = uid.split("-")
        return (bits[0], bits[-1]) if len(bits) >= 5 else None

    removed_keys = {key(u): u for u, _ in log.removed if key(u)}
    for ev in log.added:
        k = key(ev.uid)
        if k and k in removed_keys:
            log.rescheduled.append((removed_keys[k], ev.uid))
    return log


_TRACKED = ("DTSTART", "DTEND", "SUMMARY", "LOCATION", "STATUS", "DESCRIPTION")


def _field_deltas(ev: Event, prev: dict) -> List[str]:
    if prev.get(X_CONTENT_HASH) == ev.content_hash():
        return []
    rendered = {}
    for line in _vevent(ev, {}, datetime.now(timezone.utc)):
        if ":" in line:
            name, value = line.split(":", 1)
            rendered[name.split(";", 1)[0]] = value
    out = []
    for prop in _TRACKED:
        before, after = prev.get(prop), rendered.get(prop)
        if before != after:
            if prop == "DESCRIPTION":
                out.append("DESCRIPTION updated")
            else:
                out.append("{}: {} -> {}".format(prop, before or "(none)", after or "(none)"))
    return out or ["content changed"]


def print_changelog(
    log: Changelog, events: Sequence[Event], col: Collection, *, path: str, wrote: bool
) -> None:
    w = sys.stdout.write
    w("\n" + "=" * 72 + "\n")
    w("usmnt-all-levels.ics  --  {}\n".format(
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ))
    w("=" * 72 + "\n")

    ok = [r.name for r in col.results]
    w("\nSOURCES\n")
    for name in ok:
        res = next(r for r in col.results if r.name == name)
        w("  ok    {:<18} {} fixture(s), {} window(s)\n".format(
            name, len(res.fixtures), len(res.windows)))
    for name, msg in col.failures:
        w("  FAIL  {:<18} {}\n".format(name, msg))

    if col.quarantine:
        w("\nQUARANTINED (not written to the calendar -- needs a human)\n")
        for f in col.quarantine:
            w("  {} vs {}: {}\n".format(f.team, f.opponent, f.blocked_reason))

    w("\nADDED ({})\n".format(len(log.added)))
    for ev in log.added:
        w("  + {:<26} {}\n".format(ev.uid, _one_line(ev)))
    if not log.added:
        w("  (none)\n")

    w("\nCHANGED ({})\n".format(len(log.changed)))
    for ev, deltas in log.changed:
        w("  ~ {:<26} {}\n".format(ev.uid, _one_line(ev)))
        for d in deltas:
            w("      {}\n".format(d))
    if not log.changed:
        w("  (none)\n")

    w("\nREMOVED ({})\n".format(len(log.removed)))
    for uid, prev in log.removed:
        w("  - {:<26} {}\n".format(uid, prev.get("SUMMARY", "")))
    if not log.removed:
        w("  (none)\n")

    if log.rescheduled:
        w("\nLIKELY RESCHEDULES -- UID changed, subscribers keep the old event\n")
        for old, new in log.rescheduled:
            w("  ! {}  =>  {}\n".format(old, new))
        w("    The date is part of the UID, so a moved match cannot be updated\n"
          "    in place. Subscribers see the old event until they refresh a\n"
          "    calendar that no longer contains it -- most clients handle the\n"
          "    removal, but a few keep the stale copy. See README > UID scheme.\n")

    w("\nSTILL UNCONFIRMED ({})\n".format(len(log.unconfirmed)))
    for ev in log.unconfirmed:
        why = (
            "sources disagree on kickoff"
            if ev.kickoff_span
            else "competition window, no fixtures"
            if ev.is_window
            else "no kickoff time published"
        )
        w("  ? {:<26} {} ({})\n".format(ev.uid, _one_line(ev), why))
    if not log.unconfirmed:
        w("  (none)\n")

    timed = sum(1 for e in events if e.is_timed)
    w("\nSUMMARY\n")
    w("  {} events: {} timed, {} all-day, {} multi-day window(s)\n".format(
        len(events), timed,
        sum(1 for e in events if not e.is_timed and not e.is_window),
        sum(1 for e in events if e.is_window)))
    w("  {}\n".format("wrote " + path if wrote else "NOT WRITTEN (dry run)"))
    w("\n")


def _one_line(ev: Event) -> str:
    if ev.kickoff_span:
        return "{}  {} -> {}".format(
            ev.summary,
            ev.kickoff_span[0].strftime("%Y-%m-%d %H:%MZ"),
            ev.kickoff_span[1].strftime("%H:%MZ"),
        )
    if ev.kickoff_utc:
        return "{}  {}".format(ev.summary, ev.kickoff_utc.strftime("%Y-%m-%d %H:%MZ"))
    if ev.all_day_end:
        return "{}  {} .. {}".format(
            ev.summary, ev.local_date.isoformat(), ev.all_day_end.isoformat()
        )
    return "{}  {} (all-day)".format(ev.summary, ev.local_date.isoformat())


# ==========================================================================
# guards + atomic write
# ==========================================================================


class RefuseToWrite(Exception):
    """Raised when writing would damage a live subscription."""


def guard(events: Sequence[Event], col: Collection, previous: Dict[str, dict],
          *, allow_shrink: bool) -> None:
    if not col.results:
        raise RefuseToWrite(
            "every source failed ({}). Refusing to write an empty calendar over "
            "a file people are subscribed to.".format(len(col.failures))
        )
    if not events:
        raise RefuseToWrite(
            "no events survived parsing. Refusing to write -- an empty .ics "
            "would silently wipe every subscriber's calendar."
        )
    if previous and not allow_shrink:
        before, after = len(previous), len(events)
        if after < before * SHRINK_ABORT_RATIO:
            raise RefuseToWrite(
                "event count fell from {} to {} ({:.0f}% drop). This looks like "
                "a source outage, not a real schedule change. Re-run with "
                "--allow-shrink if it is genuine.".format(
                    before, after, 100 * (1 - after / before)
                )
            )


def _atomic_write(path: str, payload: bytes) -> None:
    """Write via a temp file in the same directory, then rename.

    ``os.replace`` is atomic within a filesystem, so a reader either sees the
    whole old file or the whole new one -- never a half-written calendar. The
    temp file must be a sibling or the rename stops being atomic.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    # mkstemp creates 0600. Keep whatever mode the calendar already had, so a
    # file being served over HTTP does not silently become unreadable on the
    # first refresh; fall back to 0644 for a brand new file.
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".{}.".format(os.path.basename(path)))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_ics(path: str, text: str) -> None:
    _atomic_write(path, text.encode("utf-8"))


# ==========================================================================
# main
# ==========================================================================


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true", help="rebuild from fixtures/")
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--dry-run", action="store_true", help="print the changelog, write nothing")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="permit a run that removes most of the calendar")
    ap.add_argument("--horizon-days", type=int, default=540,
                    help="drop competition-window placeholders starting more than "
                         "this many days out (default 540; Concacaf publishes to 2028)")
    args = ap.parse_args(argv)

    venues = VenueBook.load(VENUES_PATH)
    col = collect(offline=args.offline, venues=venues, horizon_days=args.horizon_days)
    events = merge(col.fixtures, col.windows)
    previous = read_previous(args.output)
    log = diff(events, previous)

    try:
        guard(events, col, previous, allow_shrink=args.allow_shrink)
    except RefuseToWrite as exc:
        print_changelog(log, events, col, path=args.output, wrote=False)
        sys.stderr.write("\nREFUSING TO WRITE: {}\n\n".format(exc))
        return 2

    wrote = False
    if not args.dry_run:
        write_ics(args.output, build_ics(events, previous, datetime.now(timezone.utc)))
        wrote = True

    print_changelog(log, events, col, path=args.output, wrote=wrote)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
