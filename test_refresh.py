#!/usr/bin/env python3
"""Parser and correctness tests.

    python3 -m unittest test_refresh -v      # or just: python3 test_refresh.py

Two kinds of fixture live in fixtures/:

  * Real captures  -- ussoccer_*.json, concacaf_competitions.json,
    fifa_u17wc_qatar2026.html. Saved verbatim from the live sources. These
    catch "the markup changed under us".
  * Synthetic      -- built inline in this file. Used for the paths the live
    data does not currently exercise (two sources disagreeing about a kickoff,
    an unknown venue, a DST boundary). They are clearly synthetic and never
    reach the generated calendar.

The UID assertions are deliberately exact. A UID is a promise to every
subscriber: change one and their client shows a duplicate event instead of an
update. If a change to venues.json or to the date logic moves a UID, these
tests are supposed to fail.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone

import refresh
import sources
from sources import (
    EmptyResultError,
    Fixture,
    Kickoff,
    ParserError,
    VenueBook,
    Window,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
VENUES = VenueBook.load(os.path.join(HERE, "venues.json"))


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def utc(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ==========================================================================
# timezone / DST -- the thing the brief called out explicitly
# ==========================================================================


class TestTimezone(unittest.TestCase):
    """U.S. DST ends 2026-11-01. The offset is computed, never written down."""

    def test_eastern_is_utc_minus_4_before_the_changeover(self):
        self.assertEqual(
            sources.local_date_of(utc(2026, 9, 30, 0, 0), "America/New_York"),
            date(2026, 9, 29),
        )
        local = utc(2026, 9, 30, 0, 0).astimezone(sources.ZoneInfo("America/New_York"))
        self.assertEqual(local.utcoffset(), timedelta(hours=-4))
        self.assertEqual(local.hour, 20)

    def test_eastern_is_utc_minus_5_after_the_changeover(self):
        local = utc(2026, 11, 20, 0, 30).astimezone(sources.ZoneInfo("America/New_York"))
        self.assertEqual(local.utcoffset(), timedelta(hours=-5))
        self.assertEqual(local.date(), date(2026, 11, 19))

    def test_the_changeover_date_itself(self):
        """2026-11-01: EDT until 02:00 local, EST after."""
        tz = sources.ZoneInfo("America/New_York")
        self.assertEqual(utc(2026, 11, 1, 5, 0).astimezone(tz).utcoffset(), timedelta(hours=-4))
        self.assertEqual(utc(2026, 11, 1, 7, 0).astimezone(tz).utcoffset(), timedelta(hours=-5))

    def test_arizona_never_shifts(self):
        """Why venue tz is a lookup and not 'US venue => Eastern'."""
        tz = sources.ZoneInfo("America/Phoenix")
        for month in (9, 12):
            self.assertEqual(
                utc(2026, month, 15, 12, 0).astimezone(tz).utcoffset(), timedelta(hours=-7)
            )

    def test_qatar_is_utc_plus_3(self):
        local = utc(2026, 11, 21, 14, 30).astimezone(sources.ZoneInfo("Asia/Qatar"))
        self.assertEqual(local.utcoffset(), timedelta(hours=3))
        self.assertEqual((local.date(), local.hour), (date(2026, 11, 21), 17))

    def test_no_hardcoded_utc_offsets_in_source(self):
        """Guard against someone 'simplifying' the tz lookup into a constant.

        Only executable code is scanned -- comments and docstrings are allowed
        to say "UTC-4", that is how the rule gets explained.
        """
        import io
        import tokenize

        banned = ("timedelta(hours=-4)", "timedelta(hours=-5)",
                  "timedelta(hours=-7)", "timedelta(hours=3)")
        for name in ("sources.py", "refresh.py"):
            with open(os.path.join(HERE, name), "r", encoding="utf-8") as fh:
                src = fh.read()
            code = "".join(
                tok.string
                for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                if tok.type not in (tokenize.COMMENT, tokenize.STRING)
            )
            for pattern in banned:
                self.assertNotIn(
                    pattern.replace(" ", ""), code.replace(" ", ""),
                    "{} hardcodes a UTC offset: {}".format(name, pattern),
                )


# ==========================================================================
# ussoccer parser, against the real saved payload
# ==========================================================================


class TestUsSoccerParser(unittest.TestCase):
    def test_senior_fixtures_and_exact_uids(self):
        res = sources.parse_ussoccer(fixture("ussoccer_usmnt.json"), VENUES, team="usmnt")
        self.assertEqual(len(res.fixtures), 4)
        self.assertEqual(res.quarantine, [])
        self.assertEqual(
            sorted(f.uid for f in res.fixtures),
            [
                "usmnt-2026-09-26-per",
                "usmnt-2026-09-29-chi",
                "usmnt-2026-10-03-mex",
                "usmnt-2026-10-06-can",
            ],
        )

    def test_uid_uses_venue_local_date_not_the_utc_date(self):
        """The Chile match is 2026-09-30 00:00Z -- 2026-09-29 in St Louis."""
        res = sources.parse_ussoccer(fixture("ussoccer_usmnt.json"), VENUES, team="usmnt")
        chile = next(f for f in res.fixtures if f.opponent_code == "chi")
        self.assertEqual(chile.kickoff.utc, utc(2026, 9, 30, 0, 0))
        self.assertEqual(chile.local_date, date(2026, 9, 29))
        self.assertEqual(chile.uid, "usmnt-2026-09-29-chi")

    def test_u17_world_cup_fixtures(self):
        res = sources.parse_ussoccer(fixture("ussoccer_u17.json"), VENUES, team="u17")
        self.assertEqual(
            sorted(f.uid for f in res.fixtures),
            ["u17-2026-11-21-mne", "u17-2026-11-24-chi", "u17-2026-11-27-alg"],
        )
        mne = next(f for f in res.fixtures if f.opponent_code == "mne")
        self.assertEqual(mne.kickoff.utc, utc(2026, 11, 21, 14, 30))
        self.assertEqual(mne.venue_tz, "Asia/Qatar")

    def test_away_fixture_is_marked(self):
        res = sources.parse_ussoccer(fixture("ussoccer_u17.json"), VENUES, team="u17")
        alg = next(f for f in res.fixtures if f.opponent_code == "alg")
        self.assertEqual(alg.home_away, "away")
        self.assertIn("(A)", alg.title)

    def test_empty_body_raises_rather_than_returning_nothing(self):
        """The rule that stops an outage from wiping the calendar."""
        with self.assertRaises(EmptyResultError):
            sources.parse_ussoccer(b"", VENUES, team="u23")
        with self.assertRaises(EmptyResultError):
            sources.parse_ussoccer(b"[]", VENUES, team="u20")

    def test_saved_empty_youth_fixtures_still_raise(self):
        for team in ("u19", "u20", "u23"):
            with self.assertRaises(EmptyResultError):
                sources.parse_ussoccer(
                    fixture("ussoccer_{}.json".format(team)), VENUES, team=team
                )

    def test_html_instead_of_json_raises(self):
        with self.assertRaises(ParserError):
            sources.parse_ussoccer(b"<!DOCTYPE html><html>", VENUES, team="usmnt")


# ==========================================================================
# "never infer" -- synthetic payloads for the paths live data does not hit
# ==========================================================================


def one_match(**over) -> bytes:
    """A single synthetic U.S. Soccer record. NOT real fixture data."""
    base = {
        "Description": "United States vs Testland",
        "Date": "2026-11-20T00:00:00",
        "Time": "01:00:00Z",
        "StageName": "Friendlies 1",
        "Competition": {"Name": "International Friendly"},
        "Venue": {
            "Id": "f3zjl2b5cqty5woe8purq000k",  # Energizer Park, America/Chicago
            "LongName": "Energizer Park",
            "Location": "St Louis, MO",
        },
        "Contestants": [
            {"Name": "United States", "Code": "USA", "Position": "home"},
            {"Name": "Testland", "ShortName": "Testland", "Code": "TST", "Position": "away"},
        ],
    }
    base.update(over)
    return json.dumps([base]).encode("utf-8")


class TestNeverInfer(unittest.TestCase):
    def test_unknown_venue_is_quarantined_not_guessed(self):
        payload = one_match(
            Venue={"Id": "brand-new-stadium-id", "LongName": "Somewhere New",
                   "Location": "Nowhere, ZZ"}
        )
        res = sources.parse_ussoccer(payload, VENUES, team="usmnt")
        self.assertEqual(res.fixtures, [])
        self.assertEqual(len(res.quarantine), 1)
        self.assertIsNone(res.quarantine[0].uid)
        self.assertIn("venues.json", res.quarantine[0].blocked_reason)

    def test_missing_opponent_code_is_quarantined_not_derived(self):
        payload = one_match(
            Contestants=[
                {"Name": "United States", "Code": "USA", "Position": "home"},
                {"Name": "Chinese Taipei", "Position": "away"},
            ]
        )
        res = sources.parse_ussoccer(payload, VENUES, team="usmnt")
        self.assertEqual(res.fixtures, [])
        self.assertIn("3-letter code", res.quarantine[0].blocked_reason)

    def test_missing_time_becomes_all_day(self):
        payload = one_match(Time="")
        res = sources.parse_ussoccer(payload, VENUES, team="usmnt")
        f = res.fixtures[0]
        self.assertIsNone(f.kickoff)
        self.assertEqual(f.local_date, date(2026, 11, 20))
        self.assertTrue(any("No kickoff time" in n for n in f.notes))

    def test_bare_midnight_utc_without_corroboration_is_unconfirmed(self):
        """00:00Z is also this source's 'time not set' sentinel."""
        payload = one_match(Time="00:00:00Z")  # no ticket URL
        res = sources.parse_ussoccer(payload, VENUES, team="usmnt")
        f = res.fixtures[0]
        self.assertIsNone(f.kickoff, "uncorroborated 00:00Z must not become a time")
        self.assertTrue(any("UNCONFIRMED" in n for n in f.notes))

    def test_midnight_utc_with_ticket_stamp_is_confirmed(self):
        payload = one_match(
            Time="00:00:00Z",
            Tickets={"URL": "https://www.ticketmaster.com/x?utm_campaign=111926_mnt_tickets"},
        )
        res = sources.parse_ussoccer(payload, VENUES, team="usmnt")
        f = res.fixtures[0]
        self.assertIsNotNone(f.kickoff)
        self.assertEqual(f.local_date, date(2026, 11, 19))
        self.assertEqual(f.uid, "usmnt-2026-11-19-tst")

    def test_ticket_stamp_disagreeing_is_reported_not_silently_preferred(self):
        payload = one_match(
            Time="01:00:00Z",
            Tickets={"URL": "https://www.ticketmaster.com/x?utm_campaign=112526_mnt_tickets"},
        )
        res = sources.parse_ussoccer(payload, VENUES, team="usmnt")
        f = res.fixtures[0]
        self.assertTrue(any("DATE DISCREPANCY" in n for n in f.notes))
        self.assertEqual(f.local_date, date(2026, 11, 19))  # from the kickoff, and said so

    def test_naive_timestamp_is_refused(self):
        """A timestamp with no zone is not a time we can use."""
        self.assertIsNone(sources._parse_iso_utc("2026-11-19T19:30:00"))
        self.assertEqual(
            sources._parse_iso_utc("2026-11-19T19:30:00Z"), utc(2026, 11, 19, 19, 30)
        )


# ==========================================================================
# concacaf windows
# ==========================================================================


class TestConcacafWindows(unittest.TestCase):
    def test_real_payload_yields_windows(self):
        res = sources.parse_concacaf_windows(
            fixture("concacaf_competitions.json"), today=date(2026, 8, 11)
        )
        self.assertTrue(res.windows)
        nov = next(
            (w for w in res.windows if w.start == date(2026, 11, 9)), None
        )
        self.assertIsNotNone(nov, "expected the Nations League NOV 2026 window")
        self.assertEqual(nov.end, date(2026, 11, 17))
        self.assertEqual(nov.uid, "window-2026-11-09-calendar-nations-league-nov-2026")

    def test_past_windows_are_dropped(self):
        res = sources.parse_concacaf_windows(
            fixture("concacaf_competitions.json"), today=date(2027, 6, 1)
        )
        self.assertTrue(all(w.end >= date(2027, 6, 1) for w in res.windows))

    def test_horizon_drops_far_future_windows(self):
        res = sources.parse_concacaf_windows(
            fixture("concacaf_competitions.json"),
            today=date(2026, 8, 11),
            horizon=date(2027, 12, 31),
        )
        self.assertTrue(all(w.start <= date(2027, 12, 31) for w in res.windows))

    def test_date_range_parsing(self):
        self.assertEqual(
            sources._parse_month_day_range("November 9 - 17", 2026),
            (date(2026, 11, 9), date(2026, 11, 17)),
        )
        self.assertEqual(
            sources._parse_month_day_range("July 24 - August 9", 2026),
            (date(2026, 7, 24), date(2026, 8, 9)),
        )
        self.assertEqual(
            sources._parse_month_day_range("December 28 - January 3", 2026),
            (date(2026, 12, 28), date(2027, 1, 3)),
        )

    def test_ambiguous_ranges_are_refused_not_approximated(self):
        for text in ("TBC", "November", "Late November", "Nov 9-17",
                     "November 9 - 17 (provisional)", "Spring"):
            self.assertIsNone(
                sources._parse_month_day_range(text, 2026),
                "{!r} should not parse".format(text),
            )

    def test_impossible_dates_are_refused(self):
        self.assertIsNone(sources._parse_month_day_range("February 30 - 31", 2026))

    def test_wrong_shape_payload_raises(self):
        with self.assertRaises(EmptyResultError):
            sources.parse_concacaf_windows(b'{"items": []}')
        with self.assertRaises(EmptyResultError):
            sources.parse_concacaf_windows(
                json.dumps({"items": [{"title": "Some Article"}]}).encode()
            )


# ==========================================================================
# fifa parser
# ==========================================================================


SYNTHETIC_FIFA = """<!DOCTYPE html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"SportsEvent",
 "name":"United States v Montenegro",
 "startDate":"2026-11-21T17:30:00+03:00",
 "location":{"name":"ASPIRE Academy Pitch 2","id":"9boxjzjmdh6tpprg145agh0id"},
 "competitor":[{"name":"United States","code":"USA"},
               {"name":"Montenegro","code":"MNE"}]}
</script></head><body></body></html>"""


class TestFifaParser(unittest.TestCase):
    def test_real_page_is_a_bot_wall_and_raises(self):
        """fifa.com serves a 4.5KB shell to non-browsers. That is not 'no matches'."""
        with self.assertRaises(EmptyResultError) as ctx:
            sources.parse_fifa_u17(fixture("fifa_u17wc_qatar2026.html"), VENUES)
        self.assertIn("bot-walled or client-rendered", str(ctx.exception))

    def test_jsonld_sportsevent_is_parsed(self):
        res = sources.parse_fifa_u17(SYNTHETIC_FIFA.encode(), VENUES)
        self.assertEqual(len(res.fixtures), 1)
        f = res.fixtures[0]
        self.assertEqual(f.uid, "u17-2026-11-21-mne")
        self.assertEqual(f.kickoff.utc, utc(2026, 11, 21, 14, 30))
        self.assertEqual(f.kickoff.source, "fifa")

    def test_non_us_events_are_ignored(self):
        html = SYNTHETIC_FIFA.replace('"United States"', '"Brazil"').replace('"USA"', '"BRA"')
        with self.assertRaises(EmptyResultError):
            sources.parse_fifa_u17(html.encode(), VENUES)


# ==========================================================================
# seed.json
# ==========================================================================


class TestSeedParser(unittest.TestCase):
    def seed(self, *entries) -> bytes:
        return json.dumps({"fixtures": list(entries)}).encode()

    def test_confirmed_entry(self):
        res = sources.parse_seed(
            self.seed({
                "team": "u20", "opponent": "Costa Rica", "opponent_code": "CRC",
                "kickoff_utc": "2026-11-15T01:00:00Z", "venue_tz": "America/Costa_Rica",
            }),
            VENUES,
        )
        f = res.fixtures[0]
        self.assertEqual(f.uid, "u20-2026-11-14-crc")
        self.assertEqual(f.kickoff.utc, utc(2026, 11, 15, 1, 0))

    def test_entry_without_time_is_all_day(self):
        res = sources.parse_seed(
            self.seed({
                "team": "u23", "opponent": "Japan", "opponent_code": "jpn",
                "local_date": "2026-10-13",
            }),
            VENUES,
        )
        f = res.fixtures[0]
        self.assertIsNone(f.kickoff)
        self.assertEqual(f.uid, "u23-2026-10-13-jpn")

    def test_naive_kickoff_is_rejected(self):
        with self.assertRaises(ParserError) as ctx:
            sources.parse_seed(
                self.seed({
                    "team": "u20", "opponent": "X", "opponent_code": "xxx",
                    "kickoff_utc": "2026-11-15T01:00:00", "venue_tz": "UTC",
                }),
                VENUES,
            )
        self.assertIn("explicit offset", str(ctx.exception))

    def test_undatable_entry_is_rejected(self):
        with self.assertRaises(ParserError) as ctx:
            sources.parse_seed(
                self.seed({"team": "u20", "opponent": "X", "opponent_code": "xxx"}), VENUES
            )
        self.assertIn("not guessable", str(ctx.exception))

    def test_bad_code_and_bad_team_are_rejected(self):
        with self.assertRaises(ParserError):
            sources.parse_seed(
                self.seed({"team": "u20", "opponent": "X", "opponent_code": "cost",
                           "local_date": "2026-11-14"}), VENUES)
        with self.assertRaises(ParserError):
            sources.parse_seed(
                self.seed({"team": "u21", "opponent": "X", "opponent_code": "crc",
                           "local_date": "2026-11-14"}), VENUES)

    def test_shipped_seed_file_is_valid_json_and_disabled(self):
        with open(os.path.join(HERE, "seed.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("fixtures", data)
        self.assertFalse(data.get("enabled"))


# ==========================================================================
# merging: the "do not pick one" rule
# ==========================================================================


def fx(team="u17", code="mne", day=date(2026, 11, 21), kickoff=None, source="a") -> Fixture:
    return Fixture(
        team=team, opponent="Montenegro", opponent_code=code, local_date=day,
        kickoff=Kickoff(utc=kickoff, source=source) if kickoff else None,
        competition="FIFA U-17 World Cup", venue="ASPIRE Academy Pitch 2",
        venue_tz="Asia/Qatar", home_away="home", source=source, source_url="http://x",
    )


class TestMergeConflicts(unittest.TestCase):
    def test_agreeing_sources_collapse_to_one_confirmed_time(self):
        k = utc(2026, 11, 21, 14, 30)
        events = refresh.merge([fx(kickoff=k, source="ussoccer"), fx(kickoff=k, source="fifa")], [])
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kickoff_utc, k)
        self.assertIsNone(ev.kickoff_span)
        self.assertFalse(ev.unconfirmed)
        self.assertEqual(ev.sources, ("fifa", "ussoccer"))

    def test_disagreeing_sources_produce_a_spanning_block(self):
        a, b = utc(2026, 11, 21, 14, 30), utc(2026, 11, 21, 17, 0)
        events = refresh.merge([fx(kickoff=a, source="ussoccer"), fx(kickoff=b, source="fifa")], [])
        ev = events[0]
        self.assertIsNone(ev.kickoff_utc, "must not pick a winner")
        self.assertEqual(ev.kickoff_span, (a, b))
        self.assertTrue(ev.unconfirmed)

    def test_the_discrepancy_is_recorded_in_the_description(self):
        a, b = utc(2026, 11, 21, 14, 30), utc(2026, 11, 21, 17, 0)
        ev = refresh.merge([fx(kickoff=a, source="ussoccer"), fx(kickoff=b, source="fifa")], [])[0]
        desc = "\n".join(ev.description_lines)
        self.assertIn("CONFLICTING KICKOFF TIMES", desc)
        self.assertIn("ussoccer reports 2026-11-21 14:30 UTC", desc)
        self.assertIn("fifa reports 2026-11-21 17:00 UTC", desc)
        self.assertIn("disagree by 2h 30m", desc)

    def test_spanning_block_covers_both_claims(self):
        a, b = utc(2026, 11, 21, 14, 30), utc(2026, 11, 21, 17, 0)
        ev = refresh.merge([fx(kickoff=a, source="ussoccer"), fx(kickoff=b, source="fifa")], [])[0]
        body = "\r\n".join(refresh._vevent(ev, {}, utc(2026, 8, 11)))
        self.assertIn("DTSTART:20261121T143000Z", body)
        self.assertIn("DTEND:20261121T190000Z", body)  # latest claim + 2h
        self.assertIn("[TIME DISPUTED]", body)
        self.assertIn("STATUS:TENTATIVE", body)

    def test_one_source_with_a_time_and_one_without_is_not_a_conflict(self):
        k = utc(2026, 11, 21, 14, 30)
        ev = refresh.merge([fx(kickoff=k, source="ussoccer"), fx(source="fifa")], [])[0]
        self.assertEqual(ev.kickoff_utc, k)
        self.assertFalse(ev.unconfirmed)

    def test_no_source_has_a_time_gives_an_all_day_event(self):
        ev = refresh.merge([fx(source="ussoccer")], [])[0]
        self.assertFalse(ev.is_timed)
        self.assertTrue(ev.unconfirmed)
        body = "\r\n".join(refresh._vevent(ev, {}, utc(2026, 8, 11)))
        self.assertIn("DTSTART;VALUE=DATE:20261121", body)
        self.assertIn("DTEND;VALUE=DATE:20261122", body)
        self.assertIn("[TIME TBD]", body)
        self.assertIn("KICKOFF TIME UNCONFIRMED", "\n".join(ev.description_lines))


# ==========================================================================
# ICS output
# ==========================================================================


class TestIcsOutput(unittest.TestCase):
    def sample(self):
        return refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="ussoccer")], [])

    def test_timed_events_are_utc_with_a_z_suffix(self):
        ics = refresh.build_ics(self.sample(), {}, utc(2026, 8, 11, 12))
        self.assertIn("DTSTART:20261121T143000Z", ics)
        self.assertIn("DTEND:20261121T163000Z", ics)
        self.assertNotIn("TZID", ics)

    def test_multi_day_window_placeholder(self):
        w = Window(key="cnl-nov", name="Concacaf Nations League",
                   start=date(2026, 11, 9), end=date(2026, 11, 17),
                   venues_text="Various", source="concacaf", source_url="http://x")
        ev = refresh.merge([], [w])[0]
        body = "\r\n".join(refresh._vevent(ev, {}, utc(2026, 8, 11)))
        self.assertIn("DTSTART;VALUE=DATE:20261109", body)
        self.assertIn("DTEND;VALUE=DATE:20261118", body)  # exclusive end
        self.assertIn("TRANSP:TRANSPARENT", body)         # does not block time
        self.assertEqual(ev.uid, "window-2026-11-09-cnl-nov")

    def test_lines_are_folded_to_75_octets(self):
        ics = refresh.build_ics(self.sample(), {}, utc(2026, 8, 11, 12))
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, repr(line[:60]))

    def test_folding_round_trips(self):
        text = "DESCRIPTION:" + ("word " * 90) + "  double  spaces  kept"
        folded = refresh._fold(text)
        unfolded = folded.replace("\r\n ", "")
        self.assertEqual(unfolded, text)

    def test_folding_does_not_split_multibyte_characters(self):
        folded = refresh._fold("LOCATION:" + "é" * 200)
        for line in folded.split("\r\n"):
            line.encode("utf-8").decode("utf-8")  # must not raise
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        self.assertEqual(folded.replace("\r\n ", ""), "LOCATION:" + "é" * 200)

    def test_text_escaping(self):
        self.assertEqual(refresh._esc("a,b;c\\d\ne"), "a\\,b\\;c\\\\d\\ne")

    def test_calendar_envelope(self):
        ics = refresh.build_ics(self.sample(), {}, utc(2026, 8, 11, 12))
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0", ics)
        self.assertEqual(ics.count("BEGIN:VEVENT"), ics.count("END:VEVENT"))


# ==========================================================================
# UID stability -- the property that matters most to subscribers
# ==========================================================================


class TestUidStability(unittest.TestCase):
    def test_uid_scheme_matches_the_documented_shape(self):
        self.assertEqual(fx(team="usmnt", code="chi", day=date(2026, 9, 29)).uid,
                         "usmnt-2026-09-29-chi")
        self.assertEqual(fx(team="u17", code="mne", day=date(2026, 11, 19)).uid,
                         "u17-2026-11-19-mne")

    def test_uid_is_lowercase_regardless_of_source_casing(self):
        self.assertEqual(fx(code="MNE").uid, "u17-2026-11-21-mne")

    def test_uid_is_none_when_a_component_is_missing(self):
        self.assertIsNone(fx(code=None).uid)
        self.assertIsNone(fx(day=None).uid)

    def test_repeated_runs_produce_identical_uids(self):
        first = [f.uid for f in sources.parse_ussoccer(
            fixture("ussoccer_usmnt.json"), VENUES, team="usmnt").fixtures]
        second = [f.uid for f in sources.parse_ussoccer(
            fixture("ussoccer_usmnt.json"), VENUES, team="usmnt").fixtures]
        self.assertEqual(first, second)

    def test_every_venue_in_venues_json_is_pinned(self):
        """Editing a tz in venues.json moves UIDs. Make that loud."""
        with open(os.path.join(HERE, "venues.json"), "r", encoding="utf-8") as fh:
            book = json.load(fh)["venues"]
        expected = {
            "3udn5yhh0bcxwmjokvt4xnm5l": "America/New_York",
            "f3zjl2b5cqty5woe8purq000k": "America/Chicago",
            "7paouifzs4wopmnhclh8nmitr": "America/Phoenix",
            "4r577lkiom42o39a9llb0jm22": "America/Chicago",
            "9boxjzjmdh6tpprg145agh0id": "Asia/Qatar",
            "77jtfrj9s350emecqwtvp6bit": "Asia/Qatar",
        }
        for vid, tz in expected.items():
            self.assertIn(vid, book)
            self.assertEqual(book[vid]["tz"], tz,
                             "venue {} tz changed -- this moves UIDs".format(vid))

    def test_dtstamp_and_sequence_are_held_when_content_is_unchanged(self):
        events = self.events()
        first = refresh.build_ics(events, {}, utc(2026, 8, 11, 12))
        previous = refresh.read_previous_text(first)
        second = refresh.build_ics(events, previous, utc(2026, 8, 12, 12))
        self.assertEqual(first, second, "an unchanged calendar must be byte-identical")

    def test_sequence_bumps_when_content_changes(self):
        first = refresh.build_ics(self.events(), {}, utc(2026, 8, 11, 12))
        previous = refresh.read_previous_text(first)
        moved = refresh.merge([fx(kickoff=utc(2026, 11, 21, 17, 0), source="ussoccer")], [])
        second = refresh.build_ics(moved, previous, utc(2026, 8, 12, 12))
        self.assertIn("SEQUENCE:0", first)
        self.assertIn("SEQUENCE:1", second)
        self.assertIn("DTSTART:20261121T170000Z", second)

    def events(self):
        return refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="ussoccer")], [])


# ==========================================================================
# diff / changelog
# ==========================================================================


class TestDiff(unittest.TestCase):
    def test_added_changed_removed_unconfirmed(self):
        before = refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="s"),
                                fx(code="chi", day=date(2026, 11, 24), source="s")], [])
        prev = refresh.read_previous_text(refresh.build_ics(before, {}, utc(2026, 8, 11)))

        after = refresh.merge([
            fx(kickoff=utc(2026, 11, 21, 17, 0), source="s"),        # changed
            fx(code="alg", day=date(2026, 11, 27), source="s"),      # added, no time
        ], [])
        log = refresh.diff(after, prev)

        self.assertEqual([e.uid for e in log.added], ["u17-2026-11-27-alg"])
        self.assertEqual([e.uid for e, _ in log.changed], ["u17-2026-11-21-mne"])
        self.assertEqual([uid for uid, _ in log.removed], ["u17-2026-11-24-chi"])
        self.assertEqual([e.uid for e in log.unconfirmed], ["u17-2026-11-27-alg"])
        self.assertIn("DTSTART", " ".join(log.changed[0][1]))

    def test_reschedule_is_flagged_because_the_uid_contains_the_date(self):
        before = refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="s")], [])
        prev = refresh.read_previous_text(refresh.build_ics(before, {}, utc(2026, 8, 11)))
        after = refresh.merge(
            [fx(day=date(2026, 11, 22), kickoff=utc(2026, 11, 22, 14, 30), source="s")], []
        )
        log = refresh.diff(after, prev)
        self.assertEqual(log.rescheduled, [("u17-2026-11-21-mne", "u17-2026-11-22-mne")])

    def test_identical_input_reports_no_changes(self):
        events = refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="s")], [])
        prev = refresh.read_previous_text(refresh.build_ics(events, {}, utc(2026, 8, 11)))
        log = refresh.diff(events, prev)
        self.assertFalse(log.has_changes)


# ==========================================================================
# write guards
# ==========================================================================


class TestGuards(unittest.TestCase):
    def col(self, results=(), failures=()):
        c = refresh.Collection()
        c.results = list(results)
        c.failures = list(failures)
        return c

    def test_refuses_when_every_source_failed(self):
        with self.assertRaises(refresh.RefuseToWrite):
            refresh.guard([], self.col(failures=[("a", "boom")]), {}, allow_shrink=False)

    def test_refuses_to_write_an_empty_calendar(self):
        ok = sources.SourceResult(name="ussoccer[usmnt]")
        with self.assertRaises(refresh.RefuseToWrite) as ctx:
            refresh.guard([], self.col(results=[ok]), {}, allow_shrink=False)
        self.assertIn("wipe", str(ctx.exception))

    def test_refuses_a_large_unexplained_shrink(self):
        ok = sources.SourceResult(name="ussoccer[usmnt]")
        events = refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="s")], [])
        previous = {"uid-{}".format(i): {} for i in range(10)}
        with self.assertRaises(refresh.RefuseToWrite) as ctx:
            refresh.guard(events, self.col(results=[ok]), previous, allow_shrink=False)
        self.assertIn("--allow-shrink", str(ctx.exception))

    def test_allow_shrink_overrides(self):
        ok = sources.SourceResult(name="ussoccer[usmnt]")
        events = refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="s")], [])
        refresh.guard(events, self.col(results=[ok]),
                      {"uid-{}".format(i): {} for i in range(10)}, allow_shrink=True)

    def test_a_partial_source_failure_still_writes(self):
        ok = sources.SourceResult(name="ussoccer[usmnt]")
        events = refresh.merge([fx(kickoff=utc(2026, 11, 21, 14, 30), source="s")], [])
        refresh.guard(events, self.col(results=[ok], failures=[("fifa", "bot wall")]),
                      {}, allow_shrink=False)


class TestAtomicWrite(unittest.TestCase):
    def test_write_replaces_the_file_and_leaves_no_temp_files(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as d:
            path = os.path.join(d, "cal.ics")
            refresh.write_ics(path, "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
            refresh.write_ics(path, "BEGIN:VCALENDAR\r\nX:2\r\nEND:VCALENDAR\r\n")
            with open(path, "r", encoding="utf-8") as fh:
                self.assertIn("X:2", fh.read())
            self.assertEqual(os.listdir(d), ["cal.ics"])

    def test_new_file_is_world_readable_not_mkstemp_0600(self):
        import stat
        import tempfile as tf
        with tf.TemporaryDirectory() as d:
            path = os.path.join(d, "cal.ics")
            refresh.write_ics(path, "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_existing_permissions_are_preserved(self):
        import stat
        import tempfile as tf
        with tf.TemporaryDirectory() as d:
            path = os.path.join(d, "cal.ics")
            refresh.write_ics(path, "A")
            os.chmod(path, 0o640)
            refresh.write_ics(path, "B")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_a_failure_mid_write_leaves_the_original_intact(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as d:
            path = os.path.join(d, "cal.ics")
            refresh.write_ics(path, "ORIGINAL")
            with self.assertRaises(TypeError):
                refresh._atomic_write(path, "not bytes")  # type: ignore[arg-type]
            with open(path, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "ORIGINAL")
            self.assertEqual(os.listdir(d), ["cal.ics"])


# ==========================================================================
# end to end, offline
# ==========================================================================


class TestEndToEnd(unittest.TestCase):
    @staticmethod
    def run_quiet(argv):
        """main() prints a full changelog; keep it out of the test output."""
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            return refresh.main(argv)

    def test_offline_run_produces_a_calendar(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as d:
            out = os.path.join(d, "usmnt-all-levels.ics")
            rc = self.run_quiet(["--offline", "--output", out])
            self.assertEqual(rc, 0)
            with open(out, "r", encoding="utf-8") as fh:
                ics = fh.read()
            for uid in ("usmnt-2026-09-29-chi", "usmnt-2026-10-06-can",
                        "u17-2026-11-21-mne", "u17-2026-11-27-alg"):
                self.assertIn("UID:{}".format(uid), ics)

    def test_second_offline_run_is_byte_identical(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as d:
            out = os.path.join(d, "usmnt-all-levels.ics")
            self.run_quiet(["--offline", "--output", out])
            with open(out, "rb") as fh:
                first = fh.read()
            self.run_quiet(["--offline", "--output", out])
            with open(out, "rb") as fh:
                second = fh.read()
            self.assertEqual(first, second, "a no-op run must not churn the file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
