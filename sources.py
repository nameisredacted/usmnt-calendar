"""One parser per source, isolated so a markup change breaks one parser, not the run.

Every parser has the same shape::

    parse_xxx(payload, venues) -> List[Fixture]        # may raise ParserError
    parse_xxx_windows(payload) -> List[Window]

and every parser obeys the same contracts:

  * Nothing is inferred. A kickoff time, date, venue or opponent code that the
    source does not state explicitly comes back as ``None``, and the caller
    turns it into an all-day placeholder. Parsers never fall back to "probably
    7pm ET".
  * A parser that finds no fixtures raises :class:`EmptyResultError`. It never
    returns ``[]`` quietly, because an empty list downstream would wipe a
    calendar that real people are subscribed to.
  * A parser that finds fixtures but cannot safely place one (unknown venue
    timezone, missing opponent code) returns it in the ``quarantine`` list on
    the result rather than dropping it or guessing.

Parsers are pure functions of ``(payload_bytes, venues)``. All network access
lives in :func:`fetch`, so every parser is testable against a saved fixture.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    raise SystemExit("This script needs Python 3.9+ for zoneinfo.")

USER_AGENT = "usmnt-calendar/1.0 (+personal ICS feed generator)"
HTTP_TIMEOUT = 30

# Team keys used in UIDs. Ordered senior -> youngest.
TEAM_KEYS = ("usmnt", "u23", "u20", "u19", "u17")

TEAM_LABELS = {
    "usmnt": "USMNT",
    "u23": "U-23 MNT",
    "u20": "U-20 MNT",
    "u19": "U-19 MNT",
    "u17": "U-17 MNT",
}

# Opta contestant ids, resolved from U.S. Soccer's public Sanity CMS
# (project oyf3dba6, dataset production, _type == "team", field optaTeamId).
# Re-discover with:
#   curl -sG --data-urlencode 'query=*[_type=="team"]{name,optaTeamId}' \
#     https://oyf3dba6.api.sanity.io/v1/data/query/production
USSOCCER_CONTESTANT_IDS = {
    "usmnt": "9vh2u1p4ppm597tjfahst2m3n",
    "u23": "2vnxw9nc0zq05fdawsdy9mc1n",
    "u20": "bo858sll0r8nayyt5smdu3pxs",
    "u19": "84g9fqkh21a3dakhx4mmrsfl5",
    "u17": "x1zdh3b7nwu0ql0uc5tgic9e",
}

USSOCCER_MATCH_API = "https://api.ussoccer.com/api/match"
CONCACAF_COMPETITIONS_API = (
    "https://dapi.concacaf.com/v2/content/en-us/competitions?$limit=25"
)
FIFA_U17_URL = "https://www.fifa.com/en/tournaments/mens/u17worldcup/qatar-2026"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ParserError(Exception):
    """Base class. Caught per-source so one bad parser cannot kill the run."""


class EmptyResultError(ParserError):
    """A parser produced no fixtures.

    This is an error, not an empty success. A source that silently returns
    nothing -- because the markup changed, because we got a bot wall, because
    a CDN served a stale shell -- must not be allowed to look like "this team
    has no matches" and delete real events from a live subscription.
    """


class FetchError(ParserError):
    """Network or HTTP-level failure."""


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Kickoff:
    """A single source's claim about when a match starts."""

    utc: datetime  # tz-aware, UTC
    source: str

    def __post_init__(self) -> None:
        if self.utc.tzinfo is None:
            raise ValueError("Kickoff.utc must be timezone-aware")


@dataclass
class Fixture:
    """One match, as claimed by one source.

    ``kickoff`` is ``None`` when the source does not state a confirmed time.
    ``local_date`` is the venue-local calendar date and is what the UID uses;
    it is ``None`` only when we could not establish it, which quarantines the
    fixture instead of letting it into the calendar with a guessed date.
    """

    team: str
    opponent: str
    opponent_code: Optional[str]  # 3 letters, taken from the source verbatim
    local_date: Optional[date]
    kickoff: Optional[Kickoff]
    competition: str
    venue: Optional[str]
    venue_tz: Optional[str]
    home_away: Optional[str]
    source: str
    source_url: str
    broadcasters: Tuple[str, ...] = ()
    tickets_url: Optional[str] = None
    notes: Tuple[str, ...] = ()
    # Populated when we could not safely place the fixture.
    blocked_reason: Optional[str] = None

    @property
    def uid(self) -> Optional[str]:
        """``{team}-{YYYY-MM-DD}-{opponent-3-letter}``.

        ``None`` when the fixture is missing either component -- callers must
        treat that as "cannot emit", never as "make something up".
        """
        if self.local_date is None or not self.opponent_code:
            return None
        return "{}-{}-{}".format(
            self.team, self.local_date.isoformat(), self.opponent_code.lower()
        )

    @property
    def title(self) -> str:
        label = TEAM_LABELS.get(self.team, self.team.upper())
        if self.home_away == "away":
            return "{} vs {} (A)".format(label, self.opponent)
        return "{} vs {}".format(label, self.opponent)


@dataclass
class Window:
    """A competition window announced before its fixtures exist.

    Becomes a multi-day all-day placeholder so the dates are blocked out in a
    subscriber's calendar even though there is no match to attach them to yet.
    """

    key: str  # slug, used for the UID
    name: str
    start: date
    end: date  # inclusive
    venues_text: Optional[str]
    source: str
    source_url: str
    notes: Tuple[str, ...] = ()

    @property
    def uid(self) -> str:
        return "window-{}-{}".format(self.start.isoformat(), self.key)


@dataclass
class SourceResult:
    """What one source produced, including what it could not place."""

    name: str
    fixtures: List[Fixture] = field(default_factory=list)
    windows: List[Window] = field(default_factory=list)
    quarantine: List[Fixture] = field(default_factory=list)


# --------------------------------------------------------------------------
# venue timezone book
# --------------------------------------------------------------------------


class VenueBook:
    """Venue-id -> IANA timezone, loaded from venues.json.

    Deliberately has no fallback. ``tz_for`` returning ``None`` is a signal to
    quarantine the fixture and tell a human to add the venue.
    """

    def __init__(self, mapping: Dict[str, dict]):
        self._map = mapping

    @classmethod
    def load(cls, path: str) -> "VenueBook":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(raw.get("venues", {}))

    def tz_for(self, venue_id: Optional[str]) -> Optional[str]:
        if not venue_id:
            return None
        entry = self._map.get(venue_id)
        return entry.get("tz") if entry else None

    def __contains__(self, venue_id: str) -> bool:
        return venue_id in self._map


def local_date_of(kickoff_utc: datetime, tz_name: str) -> date:
    """Venue-local calendar date of a UTC instant.

    The whole DST question answers itself here: ``ZoneInfo`` reads the real
    tz database, so 2026-09-29 in America/New_York is UTC-4 and 2026-11-19 is
    UTC-5 without anyone writing either number down. If the U.S. moves or
    abolishes the DST changeover, updating tzdata is the entire fix.
    """
    return kickoff_utc.astimezone(ZoneInfo(tz_name)).date()


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def fetch(url: str, *, accept: str = "*/*", referer: Optional[str] = None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FetchError("HTTP {} for {}".format(exc.code, url)) from exc
    except Exception as exc:  # noqa: BLE001 - surface anything as FetchError
        raise FetchError("{} for {}".format(exc, url)) from exc


def ussoccer_match_url(contestant_id: str, upcoming: int = 25) -> str:
    return (
        "{}?contestantId={}&year=0&upcoming={}&past=0"
        "&includeCurrent=true&penaltyShots=true"
    ).format(USSOCCER_MATCH_API, contestant_id, upcoming)


# --------------------------------------------------------------------------
# source 1: ussoccer.com  (all levels, senior + youth)
# --------------------------------------------------------------------------

# The public site renders /all-matches client-side; the data behind it is this
# JSON API. Each record carries "Date" (a UTC date at midnight) and "Time" (a
# UTC time-of-day). Together they are the UTC kickoff instant.
_TICKET_CAMPAIGN_RE = re.compile(r"utm_campaign=(\d{2})(\d{2})(\d{2})")
_TICKET_MCID_RE = re.compile(r"TIX_PG_(\d{2})(\d{2})")


def _ussoccer_ticket_local_date(match: dict) -> Optional[date]:
    """Local match date as encoded in the ticketing campaign string.

    U.S. Soccer stamps its Ticketmaster links with the local match date --
    ``utm_campaign=092926_mnt_tickets`` is 2026-09-29. That is an independent
    statement of the local date inside the same payload, which lets us tell a
    real midnight-UTC kickoff apart from a midnight-UTC "TBD" placeholder
    without guessing. Purely corroborative: we never build a date from it
    alone, we only check it against the one we computed.
    """
    tickets = match.get("Tickets") or {}
    url = tickets.get("URL") or match.get("GroupTicketUrl") or ""
    if not url:
        return None
    hit = _TICKET_CAMPAIGN_RE.search(url)
    if hit:
        mm, dd, yy = (int(g) for g in hit.groups())
        try:
            return date(2000 + yy, mm, dd)
        except ValueError:
            return None
    return None


def parse_ussoccer(
    payload: bytes,
    venues: VenueBook,
    *,
    team: str,
    source_url: str = USSOCCER_MATCH_API,
) -> SourceResult:
    """Parse one team's feed from U.S. Soccer's match API.

    Raises :class:`EmptyResultError` if the response is not a non-empty JSON
    array -- including the empty-body case, which this API returns for teams
    with no scheduled matches. The caller decides whether an empty team is
    tolerable; the parser refuses to call it success.
    """
    text = payload.decode("utf-8", "replace").strip()
    if not text:
        raise EmptyResultError(
            "ussoccer[{}]: empty response body (team has no scheduled matches, "
            "or the API changed)".format(team)
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParserError("ussoccer[{}]: response was not JSON: {}".format(team, exc))
    if not isinstance(data, list):
        raise ParserError(
            "ussoccer[{}]: expected a JSON array, got {}".format(team, type(data).__name__)
        )
    if not data:
        raise EmptyResultError("ussoccer[{}]: API returned zero matches".format(team))

    result = SourceResult(name="ussoccer[{}]".format(team))
    for match in data:
        try:
            fixture = _ussoccer_one(match, venues, team=team, source_url=source_url)
        except ParserError as exc:
            # One malformed record must not lose the other 24.
            result.quarantine.append(
                Fixture(
                    team=team,
                    opponent=str(match.get("Description", "?"))[:80],
                    opponent_code=None,
                    local_date=None,
                    kickoff=None,
                    competition="?",
                    venue=None,
                    venue_tz=None,
                    home_away=None,
                    source=result.name,
                    source_url=source_url,
                    blocked_reason=str(exc),
                )
            )
            continue
        if fixture.blocked_reason:
            result.quarantine.append(fixture)
        else:
            result.fixtures.append(fixture)

    if not result.fixtures and not result.quarantine:
        raise EmptyResultError("ussoccer[{}]: produced no fixtures".format(team))
    return result


def _ussoccer_one(
    match: dict, venues: VenueBook, *, team: str, source_url: str
) -> Fixture:
    contestants = match.get("Contestants") or []
    us = None
    opponent = None
    for c in contestants:
        name = (c.get("Name") or "").strip()
        if name.startswith("United States"):
            us = c
        else:
            opponent = c
    if opponent is None:
        raise ParserError("could not identify the opponent in Contestants")

    # 3-letter code comes from the source. We never truncate a country name to
    # make one up: "CHI"/"CHN" and "NIG"/"NGA" collide the moment you try.
    code = (opponent.get("Code") or "").strip().lower() or None
    opponent_name = (opponent.get("ShortName") or opponent.get("Name") or "").strip()

    venue_raw = match.get("Venue") or {}
    venue_id = venue_raw.get("Id")
    venue_name = venue_raw.get("LongName") or venue_raw.get("ShortName")
    venue_loc = venue_raw.get("Location")
    venue_label = (
        "{}, {}".format(venue_name, venue_loc) if venue_name and venue_loc else venue_name
    )
    tz_name = venues.tz_for(venue_id)

    competition = ((match.get("Competition") or {}).get("Name") or "").strip()
    stage = (match.get("StageName") or "").strip()
    if stage and stage.lower() not in competition.lower():
        competition = "{} - {}".format(competition, stage) if competition else stage

    home_away = None
    if us is not None:
        pos = (us.get("Position") or "").strip().lower()
        home_away = pos if pos in ("home", "away") else None

    broadcasters = tuple(
        (b.get("Name") or "").strip()
        for b in (match.get("Broadcasters") or [])
        if (b.get("Name") or "").strip()
    )
    tickets_url = (match.get("Tickets") or {}).get("URL") or match.get("GroupTicketUrl")

    notes: List[str] = []

    # --- the UTC instant -------------------------------------------------
    date_raw = match.get("Date") or ""
    time_raw = (match.get("Time") or "").strip()
    if not date_raw:
        raise ParserError("record has no Date")
    try:
        day = datetime.strptime(date_raw[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise ParserError("unparseable Date {!r}: {}".format(date_raw, exc))

    kickoff_utc: Optional[datetime] = None
    if time_raw:
        try:
            tod = datetime.strptime(time_raw.rstrip("Z"), "%H:%M:%S").time()
        except ValueError as exc:
            raise ParserError("unparseable Time {!r}: {}".format(time_raw, exc))
        kickoff_utc = datetime.combine(day, tod, tzinfo=timezone.utc)

    ticket_date = _ussoccer_ticket_local_date(match)

    # Blocked: we cannot compute a venue-local date, so we cannot build a UID.
    if tz_name is None:
        return Fixture(
            team=team,
            opponent=opponent_name,
            opponent_code=code,
            local_date=None,
            kickoff=None,
            competition=competition,
            venue=venue_label,
            venue_tz=None,
            home_away=home_away,
            source="ussoccer[{}]".format(team),
            source_url=source_url,
            broadcasters=broadcasters,
            tickets_url=tickets_url,
            blocked_reason=(
                "venue {!r} (id {}) has no timezone in venues.json -- cannot "
                "derive the local match date, so a UID would be a guess"
            ).format(venue_label or "?", venue_id),
        )

    if code is None:
        return Fixture(
            team=team,
            opponent=opponent_name,
            opponent_code=None,
            local_date=local_date_of(kickoff_utc, tz_name) if kickoff_utc else None,
            kickoff=None,
            competition=competition,
            venue=venue_label,
            venue_tz=tz_name,
            home_away=home_away,
            source="ussoccer[{}]".format(team),
            source_url=source_url,
            blocked_reason="source gave no 3-letter code for opponent {!r}".format(
                opponent_name
            ),
        )

    if kickoff_utc is None:
        # No time at all. The Date field is a bare UTC date; without a time we
        # cannot convert it to a local date, so we take it at face value and
        # say so in the event description.
        notes.append(
            "No kickoff time published by U.S. Soccer. Date shown as-is from "
            "the source (UTC calendar date); local date may differ by one day."
        )
        return Fixture(
            team=team,
            opponent=opponent_name,
            opponent_code=code,
            local_date=day,
            kickoff=None,
            competition=competition,
            venue=venue_label,
            venue_tz=tz_name,
            home_away=home_away,
            source="ussoccer[{}]".format(team),
            source_url=source_url,
            broadcasters=broadcasters,
            tickets_url=tickets_url,
            notes=tuple(notes),
        )

    local_day = local_date_of(kickoff_utc, tz_name)

    # Midnight UTC is both a legitimate kickoff (7pm CT) and the classic "time
    # not set yet" sentinel. Only the ticket campaign stamp can tell them
    # apart. Corroborated -> trust the time. Uncorroborated -> downgrade to an
    # all-day event, because the rules say an unconfirmed time is not a time.
    if kickoff_utc.hour == 0 and kickoff_utc.minute == 0:
        if ticket_date is None:
            notes.append(
                "Kickoff reported as exactly 00:00 UTC with no corroborating "
                "ticket-campaign date. 00:00Z is also this source's "
                "placeholder for an unset time, so the kickoff is treated as "
                "UNCONFIRMED and this is an all-day event."
            )
            return Fixture(
                team=team,
                opponent=opponent_name,
                opponent_code=code,
                local_date=local_day,
                kickoff=None,
                competition=competition,
                venue=venue_label,
                venue_tz=tz_name,
                home_away=home_away,
                source="ussoccer[{}]".format(team),
                source_url=source_url,
                broadcasters=broadcasters,
                tickets_url=tickets_url,
                notes=tuple(notes),
            )
        notes.append(
            "Kickoff is 00:00 UTC; confirmed as a real time by the ticket "
            "campaign stamp for {}.".format(ticket_date.isoformat())
        )

    if ticket_date is not None and ticket_date != local_day:
        # Two statements of the local date inside one payload disagree. Say so
        # rather than silently preferring either.
        notes.append(
            "DATE DISCREPANCY: kickoff {}Z converts to {} at this venue ({}), "
            "but the ticket campaign stamp says {}. UID follows the converted "
            "kickoff.".format(
                kickoff_utc.strftime("%Y-%m-%dT%H:%M"),
                local_day.isoformat(),
                tz_name,
                ticket_date.isoformat(),
            )
        )

    return Fixture(
        team=team,
        opponent=opponent_name,
        opponent_code=code,
        local_date=local_day,
        kickoff=Kickoff(utc=kickoff_utc, source="ussoccer"),
        competition=competition,
        venue=venue_label,
        venue_tz=tz_name,
        home_away=home_away,
        source="ussoccer[{}]".format(team),
        source_url=source_url,
        broadcasters=broadcasters,
        tickets_url=tickets_url,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# source 2: concacaf.com  (Nations League, U-20 Championship)
# --------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
# "November 9 - 17"  |  "July 24 - August 9"  |  "March 25 - 28"
_RANGE_SAME_MONTH = re.compile(
    r"^([A-Za-z]+)\s+(\d{1,2})\s*[-–]\s*(\d{1,2})$"
)
_RANGE_CROSS_MONTH = re.compile(
    r"^([A-Za-z]+)\s+(\d{1,2})\s*[-–]\s*([A-Za-z]+)\s+(\d{1,2})$"
)
_TITLE_YEAR = re.compile(r"\b(20\d{2})\b")

# Men's senior + men's youth competitions we care about. Women's and club
# competitions are filtered out by name; U.S. participation is not assumed.
_CONCACAF_WANTED = re.compile(
    r"^Calendar - (Nations League|U20 Championship|U20 Qualifiers|"
    r"U17 Qualifiers|Nations League Finals)\b",
    re.IGNORECASE,
)


def parse_concacaf_windows(
    payload: bytes,
    *,
    today: Optional[date] = None,
    horizon: Optional[date] = None,
) -> SourceResult:
    """Extract competition windows from Concacaf's content API.

    Concacaf publishes a window ("November 9 - 17") long before the draw
    produces fixtures. We turn those into multi-day all-day placeholders.

    The date text is free-form, so the parser is strict: it accepts only an
    unambiguous ``Month D - D`` or ``Month D - Month D`` range plus a 4-digit
    year in the title. Anything else is skipped with a note rather than
    approximated -- a placeholder on the wrong week is worse than no
    placeholder.
    """
    text = payload.decode("utf-8", "replace").strip()
    if not text:
        raise EmptyResultError("concacaf: empty response body")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParserError("concacaf: response was not JSON: {}".format(exc))

    items = data["items"] if isinstance(data, dict) and "items" in data else data
    if not isinstance(items, list) or not items:
        raise EmptyResultError("concacaf: no competition entities in response")

    result = SourceResult(name="concacaf")
    seen_any_calendar = False
    for item in items:
        title = (item.get("title") or "").strip()
        if not title.lower().startswith("calendar - "):
            continue
        seen_any_calendar = True
        if not _CONCACAF_WANTED.match(title):
            continue
        fields = item.get("fields") or {}
        if (fields.get("gender") or "").strip().lower() == "female":
            continue

        year_hit = _TITLE_YEAR.search(title)
        dates_text = (fields.get("dates") or "").strip()
        if not year_hit or not dates_text:
            continue
        span = _parse_month_day_range(dates_text, int(year_hit.group(1)))
        if span is None:
            continue
        start, end = span
        if today is not None and end < today:
            continue  # window already closed
        if horizon is not None and start > horizon:
            # Concacaf publishes windows years ahead. Blocking out a week in
            # 2028 is noise in a live subscription, and those dates move.
            continue

        slug = item.get("slug") or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        result.windows.append(
            Window(
                key=slug,
                name=(fields.get("name") or title.replace("Calendar - ", "")).strip(),
                start=start,
                end=end,
                venues_text=(fields.get("venues") or "").strip() or None,
                source="concacaf",
                source_url=(item.get("selfUrl") or CONCACAF_COMPETITIONS_API),
                notes=(
                    "Competition window announced by Concacaf as {!r}. No "
                    "fixtures published yet -- this is a placeholder for the "
                    "whole window, not a match.".format(dates_text),
                ),
            )
        )

    if not seen_any_calendar:
        raise EmptyResultError(
            "concacaf: response had {} entities but no 'Calendar - ...' items; "
            "the content model probably changed".format(len(items))
        )
    return result


def _parse_month_day_range(text: str, year: int) -> Optional[Tuple[date, date]]:
    """Strictly parse ``November 9 - 17`` / ``July 24 - August 9``.

    Returns ``None`` for anything ambiguous. A cross-month range whose end
    month is earlier than its start month rolls into the next year (a
    December -> January window).
    """
    text = text.strip()
    hit = _RANGE_SAME_MONTH.match(text)
    if hit:
        month = _MONTHS.get(hit.group(1).lower())
        if month is None:
            return None
        try:
            return date(year, month, int(hit.group(2))), date(year, month, int(hit.group(3)))
        except ValueError:
            return None
    hit = _RANGE_CROSS_MONTH.match(text)
    if hit:
        m1 = _MONTHS.get(hit.group(1).lower())
        m2 = _MONTHS.get(hit.group(3).lower())
        if m1 is None or m2 is None:
            return None
        try:
            start = date(year, m1, int(hit.group(2)))
            end = date(year + (1 if m2 < m1 else 0), m2, int(hit.group(4)))
        except ValueError:
            return None
        return start, end
    return None


# --------------------------------------------------------------------------
# source 3: fifa.com  (U-17 World Cup fixtures and kickoff times)
# --------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def parse_fifa_u17(
    payload: bytes,
    venues: VenueBook,
    *,
    source_url: str = FIFA_U17_URL,
) -> SourceResult:
    """Parse U-17 World Cup fixtures from fifa.com.

    Reads schema.org ``SportsEvent`` blocks, which is the only structured,
    contractual thing on that page; falls back to the ``__NEXT_DATA__`` blob.
    If neither is present -- which is what a bot wall or a client-rendered
    shell looks like -- it raises rather than reporting "no U.S. matches".

    This parser exists as an independent check on the U-17 kickoff times that
    U.S. Soccer publishes. When both sources have a time and the times differ,
    refresh.py emits a spanning block instead of choosing between them.
    """
    html = payload.decode("utf-8", "replace")
    events = _fifa_jsonld_events(html)
    if not events:
        events = _fifa_next_data_events(html)
    if not events:
        raise EmptyResultError(
            "fifa: no schema.org SportsEvent and no __NEXT_DATA__ in {} bytes "
            "of HTML -- page is bot-walled or client-rendered".format(len(payload))
        )

    result = SourceResult(name="fifa")
    for ev in events:
        fixture = _fifa_one(ev, venues, source_url=source_url)
        if fixture is None:
            continue
        if fixture.blocked_reason:
            result.quarantine.append(fixture)
        else:
            result.fixtures.append(fixture)

    if not result.fixtures and not result.quarantine:
        raise EmptyResultError(
            "fifa: found {} events but none involving the United States".format(len(events))
        )
    return result


def _fifa_jsonld_events(html: str) -> List[dict]:
    out: List[dict] = []
    for blob in _JSONLD_RE.findall(html):
        try:
            data = json.loads(blob.strip())
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            candidates = graph if isinstance(graph, list) else [node]
            for c in candidates:
                if isinstance(c, dict) and "SportsEvent" in str(c.get("@type", "")):
                    out.append(c)
    return out


def _fifa_next_data_events(html: str) -> List[dict]:
    hit = _NEXT_DATA_RE.search(html)
    if not hit:
        return []
    try:
        data = json.loads(hit.group(1))
    except json.JSONDecodeError:
        return []
    found: List[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "homeTeam" in node and "awayTeam" in node:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found


def _fifa_one(
    ev: dict, venues: VenueBook, *, source_url: str
) -> Optional[Fixture]:
    """Normalise one FIFA event. Returns ``None`` if the U.S. is not involved."""
    home, away = _fifa_teams(ev)
    if home is None or away is None:
        return None
    us_home = _is_us(home.get("name"))
    us_away = _is_us(away.get("name"))
    if not (us_home or us_away):
        return None
    opponent = away if us_home else home
    opponent_name = (opponent.get("name") or "").strip()
    code = (opponent.get("code") or "").strip().lower() or None

    start_raw = ev.get("startDate") or ev.get("date") or ev.get("localDate")
    kickoff_utc = _parse_iso_utc(start_raw) if start_raw else None

    venue_raw = ev.get("location") or ev.get("stadium") or {}
    if isinstance(venue_raw, str):
        venue_name, venue_id = venue_raw, None
    else:
        venue_name = (venue_raw.get("name") or "").strip() or None
        venue_id = venue_raw.get("id") or venue_raw.get("optaId")
    tz_name = venues.tz_for(venue_id) if venue_id else None

    notes: List[str] = []
    local_day: Optional[date] = None
    if kickoff_utc is not None and tz_name:
        local_day = local_date_of(kickoff_utc, tz_name)
    elif kickoff_utc is not None and start_raw and _has_explicit_offset(start_raw):
        # An offset-carrying timestamp states its own local date.
        local_day = _parse_iso_aware(start_raw).date()
        notes.append(
            "Local date taken from the source timestamp's stated UTC offset "
            "(venue {!r} is not in venues.json).".format(venue_name or "?")
        )

    if local_day is None or code is None:
        return Fixture(
            team="u17",
            opponent=opponent_name,
            opponent_code=code,
            local_date=local_day,
            kickoff=None,
            competition="FIFA U-17 World Cup",
            venue=venue_name,
            venue_tz=tz_name,
            home_away="home" if us_home else "away",
            source="fifa",
            source_url=source_url,
            blocked_reason=(
                "missing "
                + ("local date" if local_day is None else "")
                + (" and " if local_day is None and code is None else "")
                + ("opponent 3-letter code" if code is None else "")
            ),
        )

    return Fixture(
        team="u17",
        opponent=opponent_name,
        opponent_code=code,
        local_date=local_day,
        kickoff=Kickoff(utc=kickoff_utc, source="fifa") if kickoff_utc else None,
        competition="FIFA U-17 World Cup",
        venue=venue_name,
        venue_tz=tz_name,
        home_away="home" if us_home else "away",
        source="fifa",
        source_url=source_url,
        notes=tuple(notes),
    )


def _fifa_teams(ev: dict) -> Tuple[Optional[dict], Optional[dict]]:
    if "homeTeam" in ev and "awayTeam" in ev:
        return _as_team(ev["homeTeam"]), _as_team(ev["awayTeam"])
    competitors = ev.get("competitor") or ev.get("competitors") or []
    if isinstance(competitors, list) and len(competitors) >= 2:
        return _as_team(competitors[0]), _as_team(competitors[1])
    return None, None


def _as_team(node: object) -> Optional[dict]:
    if isinstance(node, str):
        return {"name": node, "code": None}
    if isinstance(node, dict):
        return {
            "name": node.get("name") or node.get("teamName") or node.get("shortName"),
            "code": node.get("code") or node.get("countryCode") or node.get("abbreviation"),
        }
    return None


def _is_us(name: Optional[str]) -> bool:
    if not name:
        return False
    n = name.strip().lower()
    return n.startswith("united states") or n in ("usa", "us")


def _parse_iso_aware(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _parse_iso_utc(value: str) -> Optional[datetime]:
    try:
        dt = _parse_iso_aware(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A naive timestamp does not state a zone. Refuse it rather than
        # assuming UTC and shifting a kickoff by hours.
        return None
    return dt.astimezone(timezone.utc)


def _has_explicit_offset(value: str) -> bool:
    v = value.strip()
    return v.endswith("Z") or bool(re.search(r"[+-]\d{2}:?\d{2}$", v))


# --------------------------------------------------------------------------
# source 4: seed.json  (hand-entered fixtures)
# --------------------------------------------------------------------------


def parse_seed(payload: bytes, venues: VenueBook, *, path: str = "seed.json") -> SourceResult:
    """Parse hand-entered fixtures from seed.json.

    Exists so a match that no scraper can reach (a youth camp friendly
    announced in a press release) can still be in the calendar, and so it goes
    through exactly the same rules as scraped data. Each entry must state its
    own confidence: ``kickoff_utc`` present means confirmed, absent means
    all-day. There is no "probably".
    """
    text = payload.decode("utf-8", "replace").strip()
    if not text:
        raise EmptyResultError("seed: {} is empty".format(path))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParserError("seed: {} is not valid JSON: {}".format(path, exc))

    entries = data.get("fixtures", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ParserError("seed: expected a list under 'fixtures'")
    if not entries:
        raise EmptyResultError("seed: no fixtures listed in {}".format(path))

    result = SourceResult(name="seed")
    for i, e in enumerate(entries):
        try:
            result.fixtures.append(_seed_one(e, venues, path=path))
        except ParserError as exc:
            raise ParserError("seed: entry #{}: {}".format(i, exc))
    return result


_SEED_REQUIRED = ("team", "opponent", "opponent_code")


def _seed_one(e: dict, venues: VenueBook, *, path: str) -> Fixture:
    for key in _SEED_REQUIRED:
        if not e.get(key):
            raise ParserError("missing required field {!r}".format(key))
    team = str(e["team"]).strip().lower()
    if team not in TEAM_KEYS:
        raise ParserError("team {!r} is not one of {}".format(team, ", ".join(TEAM_KEYS)))
    code = str(e["opponent_code"]).strip().lower()
    if len(code) != 3 or not code.isalpha():
        raise ParserError("opponent_code {!r} must be exactly 3 letters".format(code))

    tz_name = (e.get("venue_tz") or "").strip() or None
    kickoff_utc = None
    if e.get("kickoff_utc"):
        kickoff_utc = _parse_iso_utc(str(e["kickoff_utc"]))
        if kickoff_utc is None:
            raise ParserError(
                "kickoff_utc {!r} must be an ISO 8601 timestamp with an explicit "
                "offset, e.g. 2026-09-30T00:00:00Z".format(e["kickoff_utc"])
            )

    local_raw = (e.get("local_date") or "").strip()
    if local_raw:
        try:
            local_day = datetime.strptime(local_raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ParserError("local_date {!r}: {}".format(local_raw, exc))
    elif kickoff_utc is not None and tz_name:
        local_day = local_date_of(kickoff_utc, tz_name)
    else:
        raise ParserError(
            "give either local_date, or kickoff_utc together with venue_tz -- "
            "the local date is what the UID is built from and it is not guessable"
        )

    notes = tuple(str(n) for n in (e.get("notes") or []))
    if kickoff_utc is None:
        notes = notes + (
            "No confirmed kickoff time in {} -- entered as an all-day event.".format(path),
        )

    return Fixture(
        team=team,
        opponent=str(e["opponent"]).strip(),
        opponent_code=code,
        local_date=local_day,
        kickoff=Kickoff(utc=kickoff_utc, source="seed") if kickoff_utc else None,
        competition=str(e.get("competition") or "").strip() or "Unspecified",
        venue=(e.get("venue") or "").strip() or None,
        venue_tz=tz_name,
        home_away=(e.get("home_away") or "").strip().lower() or None,
        source="seed",
        source_url=str(e.get("source_url") or path),
        broadcasters=tuple(e.get("broadcasters") or []),
        notes=notes,
    )
