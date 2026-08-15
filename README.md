# usmnt-all-levels.ics

An ICS feed covering every U.S. men's national team from U-17 up: senior
USMNT, U-23/Olympic, U-20, U-19, U-17.

The interesting part of this script is not that it builds a calendar. It is
that it refuses to build a *confident* calendar out of unconfirmed data. A
sports calendar that quietly guesses is worse than one with visible holes,
because you cannot tell the guesses from the facts once they are in your
phone.

```bash
python3 refresh.py              # fetch live, write the .ics, print a changelog
python3 refresh.py --offline    # rebuild from fixtures/, no network
python3 refresh.py --dry-run    # print the changelog, write nothing
python3 -m unittest test_refresh -v
```

No third-party packages. Python 3.9+ (needs `zoneinfo`).

```
refresh.py        fetch, merge, write, diff
sources.py        one parser per source
test_refresh.py   68 tests, run against saved fixtures
venues.json       venue id -> IANA timezone   (hand-maintained, load-bearing)
seed.json         hand-entered fixtures       (hand-maintained, off by default)
fixtures/         last good response from each source
```

---

## Sources

| Source | What it gives | How it is reached |
|---|---|---|
| ussoccer.com | All levels, senior + youth | `api.ussoccer.com/api/match` |
| concacaf.com | Nations League / U-20 & U-17 windows | `dapi.concacaf.com/v2/content/en-us/competitions` |
| fifa.com | U-17 World Cup fixtures + kickoffs | page scrape, schema.org `SportsEvent` |

**ussoccer.com** — `/all-matches` renders client-side and its HTML contains no
fixtures. The data behind it is a JSON API keyed by Opta contestant id. The
five men's ids are in `sources.USSOCCER_CONTESTANT_IDS`; they came from U.S.
Soccer's public Sanity CMS and can be re-derived with:

```bash
curl -sG --data-urlencode 'query=*[_type=="team"]{name,optaTeamId}' https://oyf3dba6.api.sanity.io/v1/data/query/production
```

Each record carries `Date` (a UTC date) and `Time` (a UTC time-of-day); together
they are the kickoff instant.

**concacaf.com** — publishes competition *windows* ("November 9 - 17") long
before a draw produces fixtures. Those become the multi-day placeholders.

**fifa.com** — currently serves a 4.5 KB shell to anything without a browser
fingerprint, so **this parser produces nothing today**. It is written against
schema.org `SportsEvent` with a `__NEXT_DATA__` fallback, and it is tested
against a synthetic payload plus the real bot-wall response (which it must
reject loudly rather than report as "no matches"). It exists as the
independent cross-check on U-17 kickoff times — the moment it returns data,
any disagreement with U.S. Soccer surfaces automatically as a disputed block.

### Current state of each level

| Level | Fixtures now | Note |
|---|---|---|
| USMNT | 4 | Sept–Oct 2026 friendlies |
| U-17 | 3 | U-17 World Cup group stage, Doha |
| U-23, U-20, U-19 | 0 | API returns an empty body — no fixtures scheduled |

An empty youth feed is reported as a source failure, not silently accepted.
That is deliberate: "this team has no matches" and "the API changed" look
identical from the outside, and only one of them should ever delete events.

---

## UID scheme

```
{team}-{YYYY-MM-DD}-{opponent-3-letter}

usmnt-2026-09-29-chi
u17-2026-11-21-mne
window-2026-11-09-calendar-nations-league-nov-2026
```

- `team` — `usmnt`, `u23`, `u20`, `u19`, `u17`.
- date — the **venue-local** match date, not the UTC date. The Chile friendly
  is `2026-09-30T00:00Z`, which is the evening of the **29th** in St. Louis, so
  the UID is `...-2026-09-29-chi`.
- opponent code — the 3-letter code **as published by the source**, lowercased.
  Never derived from the country name: truncation collides (CHI/CHN, NIG/NGA).
  No code from the source means the fixture is quarantined, not guessed at.

There is no `@domain` suffix. RFC 5545 suggests one, but the scheme above is
the contract, and appending a domain later would change every UID and
duplicate every event. If you ever want one, it has to be a deliberate
one-time migration.

### The reschedule caveat

The date is *inside* the UID, so **a postponed match cannot be updated in
place** — it leaves as one UID and arrives as another. `refresh.py` detects
this (same team, same opponent, different date) and prints:

```
LIKELY RESCHEDULES -- UID changed, subscribers keep the old event
  ! usmnt-2026-09-29-chi  =>  usmnt-2026-09-30-chi
```

Most clients drop the vanished event on the next refresh; a few keep a stale
copy. The alternative — keying on a source match id — would survive
reschedules but makes the UID opaque and ties it to one source's identifiers
forever. This is the tradeoff the requested scheme takes; the script's job is
to make it visible rather than silent.

---

## Correctness rules, and where each one lives

**Never infer a kickoff time, date, or venue.**
Missing time → all-day event, with the uncertainty spelled out in
`DESCRIPTION`. Missing 3-letter code or unknown venue timezone → the fixture
is *quarantined*: kept out of the calendar, printed under `QUARANTINED` in the
changelog. It is never dropped silently and never filled in with a default.

One case needs explaining. U.S. Soccer reports some kickoffs as exactly
`00:00:00Z`, which is both a real time (7pm Central) and the classic
"not set yet" sentinel. The tiebreak is the ticket link: U.S. Soccer stamps it
with the local match date (`utm_campaign=092926_mnt_tickets` → 2026-09-29). If
that stamp corroborates the converted date, the time is real. If there is no
stamp, the kickoff is treated as **unconfirmed** and the event goes out
all-day. Erring toward all-day is the safe direction.

**If two sources disagree on a time, do not pick one.**
`_merge_group` in `refresh.py`. The event becomes a block spanning every
reported kickoff, `SUMMARY` is prefixed `[TIME DISPUTED]`, `STATUS` is
`TENTATIVE`, and `DESCRIPTION` names each source and its claim:

```
CONFLICTING KICKOFF TIMES -- this block spans every reported time and is NOT a single confirmed kickoff.
  ussoccer reports 2026-11-21 14:30 UTC
  fifa reports 2026-11-21 17:00 UTC
  Sources disagree by 2h 30m. No source has been preferred.
```

**A parser that returns nothing must raise.**
Every parser raises `EmptyResultError` rather than returning `[]`. Sources run
in isolation, so one failure is recorded and the run continues — but
`guard()` refuses to write if *every* source failed, if zero events survived,
or if the event count dropped by more than half (`--allow-shrink` to override).
A source outage cannot empty a live subscription.

**Write to a temp file and rename.**
`_atomic_write` creates a temp file *in the same directory* (so the rename
stays within one filesystem and stays atomic), `fsync`s it, then `os.replace`s
it into position. A reader sees the whole old file or the whole new one.

**Diff and print a changelog.**
Added / changed (with the specific fields) / removed / still-unconfirmed, plus
quarantined fixtures, per-source status, and likely reschedules.

---

## Time handling

Timed events are emitted in UTC with a `Z` suffix — no `VTIMEZONE`, nothing for
a client to misread.

**No UTC offset is written down anywhere.** Offsets come from `zoneinfo`
reading the system tz database. U.S. DST ends 2026-11-01, so Eastern is UTC-4
before and UTC-5 after, and that falls out of the lookup without anyone
encoding either number. `test_no_hardcoded_utc_offsets_in_source` tokenizes
both modules and fails if a literal offset appears in executable code.

This also handles the cases a single "ET" assumption gets wrong:

- **America/Phoenix** never shifts. The Mexico friendly at State Farm Stadium
  is UTC-7 in September *and* December.
- **Asia/Qatar** is UTC+3 year round, for the U-17 World Cup.

`MATCH_DURATION` (2 hours) is the one assumption in the file. It only ever
supplies `DTEND` for a timed event — it never affects a UID, a date, or a start
time.

### venues.json

Getting the venue-local date requires the venue's timezone, and guessing it
("a US venue means Eastern") silently produces a wrong local date for a night
game, and therefore a wrong UID, and therefore a duplicate event for every
subscriber. So `venues.json` maps Opta venue id → IANA timezone by hand, and a
venue that is not in it **quarantines the fixture** with a message telling you
to add it.

Keyed on the Opta id, not the name, because stadium names get resold (Busch →
Energizer Park) while ids do not. **Editing a `tz` value moves UIDs**, so
`test_every_venue_in_venues_json_is_pinned` asserts all six current values and
fails on purpose if one changes.

---

## seed.json

For matches no scraper can reach — a youth camp friendly announced only in a
press release. Set `"enabled": true` and add entries; they go through the same
rules as scraped data:

- Omit `kickoff_utc` if the time is not confirmed → all-day event. Do not enter
  an approximate time; in a calendar it is indistinguishable from a real one.
- `kickoff_utc` must carry an explicit offset. A naive timestamp is rejected,
  not assumed to be UTC.
- `opponent_code` must be exactly 3 letters, from the source.
- Supply either `local_date`, or `kickoff_utc` **and** `venue_tz`.

A seeded fixture that collides with a scraped one is merged, and a kickoff
disagreement surfaces as a disputed block — which makes this a useful way to
cross-check a source you do not fully trust.

---

## Output details

- Line folding at 75 octets, never mid-UTF-8-character; round-trip tested.
- `DTEND` for all-day events is exclusive, per RFC 5545 (a one-day event ends
  the next day).
- Window placeholders are `TRANSP:TRANSPARENT` so they do not show you as busy.
- `STATUS:TENTATIVE` on anything unconfirmed, `CONFIRMED` otherwise.
- `X-USMNT-CONTENT-HASH` holds a hash of the visible content. `DTSTAMP` and
  `SEQUENCE` are carried forward when it is unchanged, so **a no-op run
  produces a byte-identical file** instead of churning every event. `SEQUENCE`
  increments only on a real change, which is what tells a client this is an
  update rather than a new event.

## Hosting (GitHub)

Calendar apps only auto-update from an `http(s)` URL. Opening the `.ics` file
directly just imports a frozen snapshot that later refreshes never touch — so
the file has to be reachable over the web for a subscription to mean anything.

This repo is the host: the calendar is committed, and the subscription URL is
GitHub's raw view of it.

```
https://raw.githubusercontent.com/<user>/<repo>/main/usmnt-all-levels.ics
```

One-time setup:

```bash
git init -b main
git add .
git commit -m "USMNT all-levels calendar"
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main
```

Then subscribe (macOS Calendar → File → New Calendar Subscription; iOS →
Settings → Calendar → Accounts → Add Account → Other → Add Subscribed
Calendar). Set the app's own refresh to daily — it is independent of, and
should be no slower than, the monthly job here.

Notes and limits:

- **The repo must be public** for `raw.githubusercontent.com` to serve the file
  without a token. That makes the calendar publicly readable — it is public
  fixture data, but the repo will also carry your fixtures and logs, so do not
  put anything private in here.
- **Raw URLs are CDN-cached ~5 minutes.** Irrelevant at a monthly cadence.
- **Use the `raw.githubusercontent.com` host**, not a `github.com/.../blob/...`
  page — the latter serves HTML and the subscription will fail to parse.
- Pin the branch (`main`), not a commit SHA, or the URL stops updating.

### Credentials for the automated push

`refresh-monthly.sh` commits and pushes when the calendar changes. launchd runs
it with no terminal, so git must authenticate without prompting. There are
currently **no SSH keys on this machine**, so pick one:

```bash
# SSH (recommended for automation)
ssh-keygen -t ed25519 -C "usmnt-calendar"    # empty passphrase, or add to keychain
pbcopy < ~/.ssh/id_ed25519.pub               # paste into GitHub > Settings > SSH keys
ssh -T git@github.com                        # verify
```

```bash
# or HTTPS + personal access token, stored once in the macOS keychain
git remote set-url origin https://github.com/<user>/<repo>.git
git config --global credential.helper osxkeychain
git push -u origin main                      # username = GitHub user, password = PAT
```

The push runs with `GIT_TERMINAL_PROMPT=0`, so missing credentials fail fast
and get logged rather than hanging a background job forever. Until an `origin`
remote exists the publish step is skipped and logged; the refresh still runs.

## Scheduling it

Installed as a launchd agent that runs **monthly, on the 1st at 09:00 local**:

```
~/Library/LaunchAgents/local.usmnt-calendar.refresh.plist   the schedule
refresh-monthly.sh                                          the wrapper
refresh.log                                                 timestamped output
```

launchd rather than cron: cron is deprecated on macOS, and a cron job whose
time passes while the Mac is asleep is simply skipped, whereas launchd runs a
missed `StartCalendarInterval` job once on the next wake.

```bash
launchctl list | grep usmnt                                  # is it loaded?
launchctl kickstart -p gui/$UID/local.usmnt-calendar.refresh # run it now
tail -40 refresh.log                                         # what happened
```

**Changing the cadence** — edit `StartCalendarInterval` in the plist, then
reload it. Drop the `Day` key for daily; use an array of dicts for several
times. Weekly (`Weekday`, 0 = Sunday) is a better fit if you care about
kickoff times, which get confirmed in the weeks before a window:

```xml
<key>StartCalendarInterval</key>
<dict>
    <key>Weekday</key><integer>1</integer>
    <key>Hour</key><integer>9</integer>
    <key>Minute</key><integer>0</integer>
</dict>
```

```bash
launchctl bootout gui/$UID/local.usmnt-calendar.refresh
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/local.usmnt-calendar.refresh.plist
```

**Uninstalling** — `launchctl bootout` as above, then delete the plist.

Exit codes from `refresh.py`: `0` wrote (or dry-run), `2` refused to write —
the changelog in `refresh.log` says why.

The wrapper logs the two steps separately, so `grep -E 'refresh:|publish:|result:' refresh.log`
gives a quick history:

```
refresh: ok
publish: no change to usmnt-all-levels.ics, nothing to push
result: ok
```

`result: ATTENTION NEEDED` means either the refresh refused to write or the
push failed — in the latter case the commit exists locally and a manual
`git push origin HEAD` will catch it up. The log self-trims to 3000 lines.

## Known limitations

- **fifa.com returns nothing** behind its bot wall, so U-17 kickoff times are
  currently single-sourced and the conflict path is exercised only by tests.
- **U-23 / U-20 / U-19 have no fixtures** in U.S. Soccer's API right now, so
  those levels are wired up and tested but empty. Use `seed.json` in the
  meantime.
- Concacaf window text is free-form. Only unambiguous `Month D - D` and
  `Month D - Month D` ranges are accepted; anything else ("TBC", "Late
  November") is skipped rather than approximated.
- `--horizon-days` (default 540) drops window placeholders further out than
  ~18 months; Concacaf publishes to 2028 and those dates move.
- Opponent names come through as the source spells them, so a source rename
  ("Turkey" → "Türkiye") shows up as a changed `SUMMARY`, not a new event —
  the UID is built from the code, not the name.
