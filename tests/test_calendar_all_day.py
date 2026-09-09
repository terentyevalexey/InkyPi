import os
import sys
from datetime import datetime

import icalendar
import pytest
import pytz

# The calendar plugin imports siblings as top-level modules ("utils.app_utils"),
# so the src directory has to be importable in its own right.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from plugins.calendar.calendar import Calendar  # noqa: E402

DAY = "2026-09-08"


@pytest.fixture
def calendar():
    # generate_image is never called, so the plugin's dependencies are not needed.
    return Calendar.__new__(Calendar)


def all_day(title=None, start=DAY, end=None):
    event = {"title": title or start, "start": start, "backgroundColor": "#007bff",
             "textColor": "#ffffff", "allDay": True}
    if end:
        event["end"] = end
    return event


def timed(start=f"{DAY}T10:00:00+03:00"):
    return {"title": "meeting", "start": start, "backgroundColor": "#007bff",
            "textColor": "#ffffff", "allDay": False}


class TestAllDayLayout:

    @pytest.mark.parametrize(
        "count,lines,visible,overflow",
        [
            (0, 0, 0, None),    # no row at all
            (1, 1, 1, None),
            (5, 1, 5, None),    # exactly one line
            (6, 2, 6, None),    # spills onto a second line
            (10, 2, 10, None),  # both lines exactly full
            (11, 2, 9, 2),      # last cell becomes the chip
            (20, 2, 9, 11),
        ],
    )
    def test_row_is_capped_at_two_lines(self, calendar, count, lines, visible, overflow):
        events = [all_day(f"e{i}") for i in range(count)]
        kept, actual_lines, _ = calendar.layout_all_day_events(events, {})

        chips = [e for e in kept if e.get("overflowCount")]
        shown = [e for e in kept if e["allDay"] and not e.get("overflowCount")]

        assert actual_lines == lines
        assert len(shown) == visible
        assert [c["overflowCount"] for c in chips] == ([overflow] if overflow else [])
        # The chip always accounts for exactly what was dropped.
        assert len(shown) + sum(c["overflowCount"] for c in chips) == count

    def test_timed_events_are_never_dropped(self, calendar):
        events = [all_day(f"e{i}") for i in range(20)] + [timed()]
        kept, _, _ = calendar.layout_all_day_events(events, {})
        assert sum(1 for e in kept if not e["allDay"]) == 1

    def test_multi_day_event_survives_a_crowded_day(self, calendar):
        trip = all_day("trip", DAY, "2026-09-11")
        events = [trip] + [all_day(f"e{i}") for i in range(15)]
        kept, _, _ = calendar.layout_all_day_events(events, {})
        assert any(e["title"] == "trip" for e in kept)

    def test_week_views_stack_one_event_per_line(self, calendar):
        # A week gives each day its own narrow column, so five events cannot sit
        # side by side the way they do in the single-column day view.
        events = [all_day(f"e{i}") for i in range(5)]
        kept, lines, _ = calendar.layout_all_day_events(events, {}, "timeGridWeek")
        shown = [e for e in kept if not e.get("overflowCount")]
        chips = [e for e in kept if e.get("overflowCount")]
        assert (lines, len(shown)) == (2, 1)
        assert chips[0]["overflowCount"] == 4

    @pytest.mark.parametrize(
        "count,lines,columns",
        [
            (1, 1, 1),    # a lone event spans the row, like a lone timed event
            (2, 1, 2),
            (5, 1, 5),
            (6, 2, 3),    # balanced across two lines rather than 5 + 1
            (7, 2, 4),    # 4 + 3
            (10, 2, 5),
            (11, 2, 5),   # 9 events plus the chip still fills 5 + 5
        ],
    )
    def test_columns_follow_the_events_actually_present(self, calendar, count, lines, columns):
        events = [all_day(f"e{i}") for i in range(count)]
        _, actual_lines, actual_columns = calendar.layout_all_day_events(events, {})
        assert (actual_lines, actual_columns) == (lines, columns)

    def test_no_all_day_events_needs_no_columns(self, calendar):
        assert calendar.layout_all_day_events([timed()], {}) == ([timed()], 0, 1)

    @pytest.mark.parametrize("value,expected", [("3", 3), ("", 5), (None, 5), ("abc", 5), ("0", 1)])
    def test_per_line_setting_is_tolerant_of_junk(self, calendar, value, expected):
        assert calendar.get_all_day_per_line({"allDayMaxPerLine": value}) == expected

    @pytest.mark.parametrize(
        "event,days",
        [
            (all_day("x", DAY), [DAY]),                                    # no DTEND
            (all_day("x", DAY, "2026-09-11"), [DAY, "2026-09-09", "2026-09-10"]),  # DTEND exclusive
            (all_day("x", DAY, DAY), [DAY]),                               # degenerate DTEND
        ],
    )
    def test_all_day_span(self, event, days):
        assert [d.isoformat() for d in Calendar.all_day_span(event)] == days


class TestIcsColors:

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("palevioletred", "#db7093"),  # RFC 7986 CSS3 name
            ("khaki", "#f0e68c"),
            ("#0f9edb", "#0f9edb"),        # X-APPLE-CALENDAR-COLOR hex
            ("not-a-color", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse_ics_color(self, calendar, value, expected):
        assert calendar.parse_ics_color(value) == expected


def ics_resource(uid, tzid="Europe/Moscow"):
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VTIMEZONE
TZID:{tzid}
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0300
TZOFFSETTO:+0300
TZNAME:MSK
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:{uid}
DTSTAMP:20260908T090000Z
DTSTART;TZID={tzid}:20260908T100000
DTEND;TZID={tzid}:20260908T110000
SUMMARY:{uid}
END:VEVENT
END:VCALENDAR"""


def multistatus(*resources):
    blobs = "".join(
        f"<D:response><D:propstat><D:prop>"
        f"<C:calendar-data>{r}</C:calendar-data>"
        f"</D:prop></D:propstat></D:response>"
        for r in resources
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"{blobs}</D:multistatus>"
    ).encode("utf-8")


class TestCalendarSources:

    def test_each_calendar_keeps_its_own_credentials(self, calendar):
        sources = calendar.get_calendar_sources({
            "calendarURLs[]": ["https://a.example/cal", "https://b.example/cal"],
            "calendarColors[]": ["#111111", "#222222"],
            "calendarUsernames[]": ["ann", ""],
            "calendarPasswords[]": ["secret", ""],
            "calendarTypes[]": ["caldav", "ics"],
        })
        assert [s["auth"] for s in sources] == [("ann", "secret"), None]
        assert [s["caldav"] for s in sources] == [True, False]
        assert [s["color"] for s in sources] == ["#111111", "#222222"]

    def test_a_password_is_not_shared_with_other_hosts(self, calendar):
        # The regression this guards: one login broadcast to every calendar.
        sources = calendar.get_calendar_sources({
            "calendarURLs[]": ["https://mine.example/cal", "https://theirs.example/cal"],
            "calendarUsernames[]": ["ann", ""],
            "calendarPasswords[]": ["secret", ""],
        })
        assert sources[1]["auth"] is None

    def test_legacy_shared_login_still_works(self, calendar):
        # Instances saved before per-calendar credentials must keep fetching.
        sources = calendar.get_calendar_sources({
            "calendarURLs[]": ["https://a.example/cal", "https://b.example/cal"],
            "loginUsername": "ann",
            "loginPassword": "secret",
        })
        assert [s["auth"] for s in sources] == [("ann", "secret")] * 2

    def test_short_arrays_do_not_misalign(self, calendar):
        sources = calendar.get_calendar_sources({
            "calendarURLs[]": ["https://a.example/cal", "https://b.example/cal"],
            "calendarColors[]": ["#111111"],
        })
        assert len(sources) == 2
        assert sources[1]["color"] == "#007BFF"


class TestCaldav:

    def test_timestamps_are_utc_basic_format(self, calendar):
        import pytz
        tz = pytz.timezone("Europe/Moscow")
        naive = datetime(2026, 9, 8, 0, 0)
        assert calendar.caldav_timestamp(naive, tz) == "20260907T210000Z"

    def test_resources_merge_into_one_calendar(self, calendar):
        merged = calendar.merge_caldav_response(
            multistatus(ics_resource("a"), ics_resource("b"))
        )
        events = [c for c in merged.subcomponents if c.name == "VEVENT"]
        timezones = [c for c in merged.subcomponents if c.name == "VTIMEZONE"]
        assert len(events) == 2
        # Both resources carry the same VTIMEZONE; it must not be duplicated.
        assert len(timezones) == 1

    def test_distinct_timezones_are_both_kept(self, calendar):
        merged = calendar.merge_caldav_response(
            multistatus(ics_resource("a", "Europe/Moscow"), ics_resource("b", "UTC"))
        )
        assert len([c for c in merged.subcomponents if c.name == "VTIMEZONE"]) == 2

    def test_empty_response_yields_an_empty_calendar(self, calendar):
        merged = calendar.merge_caldav_response(multistatus())
        assert [c for c in merged.subcomponents if c.name == "VEVENT"] == []

    def test_malformed_xml_is_reported_clearly(self, calendar):
        with pytest.raises(RuntimeError, match="malformed XML"):
            calendar.merge_caldav_response(b"<D:multistatus")


class TestViewRange:

    def test_day_view_follows_the_offset_date(self, calendar):
        # The offset is applied by the caller, so the range simply tracks view_dt.
        start, end, initial = calendar.get_view_range(
            "timeGridDay", datetime(2026, 9, 9), {})
        assert (start, end) == (datetime(2026, 9, 9), datetime(2026, 9, 10))
        assert initial.isoformat() == "2026-09-09"

    @pytest.mark.parametrize("week_start_day,expected", [("1", "2026-09-07"), ("0", "2026-09-06")])
    def test_week_start_honours_the_first_day_setting(self, calendar, week_start_day, expected):
        # 2026-09-08 is a Tuesday.
        start = calendar.get_week_start(datetime(2026, 9, 8), {"weekStartDay": week_start_day})
        assert start.date().isoformat() == expected

    def test_multi_week_grid_spans_past_present_and_future(self, calendar):
        settings = {"weekStartDay": "1", "displayPastWeeks": "1", "displayWeeks": "4"}
        start, end, initial = calendar.get_view_range("dayGrid", datetime(2026, 9, 8), settings)
        assert start == datetime(2026, 8, 31)          # one week before the current week
        assert (end - start).days == 6 * 7             # 1 past + current + 4 ahead
        assert initial == start.date()
        assert calendar.get_day_grid_weeks(settings) == 6

    def test_multi_week_grid_can_drop_the_past_row(self, calendar):
        settings = {"weekStartDay": "1", "displayPastWeeks": "0", "displayWeeks": "2"}
        start, _, _ = calendar.get_view_range("dayGrid", datetime(2026, 9, 8), settings)
        assert start == datetime(2026, 9, 7)           # the current week itself
        assert calendar.get_day_grid_weeks(settings) == 3

    def test_default_span_is_one_past_week_and_three_ahead(self, calendar):
        # Five rows keeps the cells big enough to read on a 7.3" panel.
        assert calendar.get_day_grid_span({}) == (1, 3)
        assert calendar.get_day_grid_weeks({}) == 5

    def test_fetch_window_matches_the_rendered_span(self, calendar):
        # A mismatch here silently drops events off the edge of the grid.
        settings = {"weekStartDay": "1", "displayPastWeeks": "2", "displayWeeks": "3"}
        start, end, _ = calendar.get_view_range("dayGrid", datetime(2026, 9, 8), settings)
        assert (end - start).days == calendar.get_day_grid_weeks(settings) * 7


class TestCaldavHardening:

    def test_entity_declarations_are_refused(self, calendar):
        # ElementTree expands internal entities, so a nested-entity bomb from a
        # hostile or compromised calendar server could stall the device.
        bomb = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
            b'<D:multistatus xmlns:D="DAV:"><D:response>&lol2;</D:response></D:multistatus>'
        )
        with pytest.raises(RuntimeError, match="entities"):
            calendar.merge_caldav_response(bomb)

    def test_ordinary_responses_still_parse(self, calendar):
        merged = calendar.merge_caldav_response(multistatus(ics_resource("a")))
        assert len([c for c in merged.subcomponents if c.name == "VEVENT"]) == 1


NEXTCLOUD_LISTING = b'''<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav"
               xmlns:x1="http://apple.com/ns/ical/">
  <d:response>
    <d:href>/remote.php/dav/calendars/ann/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/ann/personal/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>
      <d:displayname>Personal</d:displayname>
      <x1:calendar-color>#0f9edb</x1:calendar-color>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/ann/shared/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>
      <d:displayname>Shared</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/ann/inbox/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
      <d:displayname>Inbox</d:displayname>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>'''


class TestCaldavDiscovery:

    def test_only_calendar_collections_are_returned(self, calendar):
        found = calendar.caldav_collections(
            NEXTCLOUD_LISTING, "https://cloud.example", "/remote.php/dav/calendars/ann/")
        names = [c["name"] for c in found]
        # The home collection itself and the non-calendar inbox are both skipped.
        assert names == ["Personal", "Shared"]
        assert found[0]["url"] == "https://cloud.example/remote.php/dav/calendars/ann/personal/"

    def test_calendar_colour_is_picked_up_when_published(self, calendar):
        found = calendar.caldav_collections(
            NEXTCLOUD_LISTING, "https://cloud.example", "/remote.php/dav/calendars/ann/")
        assert found[0]["color"] == "#0f9edb"
        assert found[1]["color"] is None      # server published none

    def test_discovery_listing_rejects_entity_declarations(self, calendar):
        with pytest.raises(RuntimeError, match="entities"):
            calendar.caldav_collections(
                b'<!DOCTYPE x [<!ENTITY a "a">]><d:multistatus xmlns:d="DAV:"/>',
                "https://cloud.example", "/x/")

    def test_discover_action_needs_a_url(self, calendar):
        with pytest.raises(RuntimeError, match="URL is required"):
            calendar.action_discover({})


class _Resp:
    def __init__(self, content):
        self.content = content


PRINCIPAL_RESPONSE = b'''<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:"><d:response><d:href>/remote.php/dav/</d:href>
<d:propstat><d:prop><d:current-user-principal>
<d:href>/remote.php/dav/principals/users/ann/</d:href>
</d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>'''

HOME_RESPONSE = b'''<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/remote.php/dav/principals/users/ann/</d:href>
<d:propstat><d:prop><cal:calendar-home-set>
<d:href>/remote.php/dav/calendars/ann/</d:href>
</cal:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>'''


class TestCaldavHrefNamespaces:

    def test_principal_href_is_read_from_the_dav_namespace(self, calendar):
        assert calendar.caldav_href(_Resp(PRINCIPAL_RESPONSE), "D:current-user-principal") \
            == "/remote.php/dav/principals/users/ann/"

    def test_home_set_href_is_read_from_the_caldav_namespace(self, calendar):
        # calendar-home-set is CalDAV, not DAV; looking for it under DAV silently
        # returns nothing and discovery reports "no calendars found".
        assert calendar.caldav_href(_Resp(HOME_RESPONSE), "C:calendar-home-set") \
            == "/remote.php/dav/calendars/ann/"

    def test_wrong_namespace_finds_nothing(self, calendar):
        assert calendar.caldav_href(_Resp(HOME_RESPONSE), "D:calendar-home-set") is None


class TestFetchResilience:

    def _sources(self, count=2):
        return [{"url": f"https://cal{i}.example/x", "color": "#007bff",
                 "auth": None, "caldav": False, "busy": True} for i in range(count)]

    def test_one_dead_calendar_does_not_blank_the_display(self, calendar, monkeypatch):
        import pytz
        good = [{"title": "meeting", "start": f"{DAY}T10:00:00+03:00", "allDay": False}]

        def fetch(source, tz, start, end):
            if source["url"].startswith("https://cal0"):
                raise RuntimeError("No route to host")
            return "ok"

        monkeypatch.setattr(calendar, "fetch_calendar", fetch)
        monkeypatch.setattr("plugins.calendar.calendar.recurring_ical_events",
                            type("M", (), {"of": staticmethod(
                                lambda cal: type("B", (), {"between": staticmethod(
                                    lambda s, e: [])})())}))

        events, failed = calendar.fetch_ics_events(
            self._sources(), pytz.utc, datetime(2026, 9, 8), datetime(2026, 9, 9), {})
        assert events == []                        # reached the end, not an error
        assert [f["host"] for f in failed] == ["cal0.example"]  # named the dropout

    def test_all_calendars_failing_is_still_an_error(self, calendar, monkeypatch):
        import pytz

        def fetch(source, tz, start, end):
            raise RuntimeError("No route to host")

        monkeypatch.setattr(calendar, "fetch_calendar", fetch)
        with pytest.raises(RuntimeError, match="No calendar could be reached"):
            calendar.fetch_ics_events(
                self._sources(), pytz.utc, datetime(2026, 9, 8), datetime(2026, 9, 9), {})


class TestLocaleScript:

    @pytest.mark.parametrize(
        "language,expected",
        [
            ("ru", "ru.global.min.js"),
            ("de-at", "de-at.global.min.js"),
            ("en", None),          # the main bundle already carries English
            ("", None),
            (None, None),
            ("../../../etc/passwd", None),   # not a known locale, so never a path
            ("zz", None),
        ],
    )
    def test_only_a_known_locale_is_loaded(self, calendar, language, expected):
        assert calendar.get_locale_script({"language": language}) == expected


# Moscow, matching the device: a fixed-offset zone, so the arithmetic is checkable
# by eye. A DST-transition case is covered separately with a zone that has one.
TZ = pytz.timezone("Europe/Moscow")
GRID = {"startTimeInterval": "10", "endTimeInterval": "20"}


def at(hhmm, day=DAY):
    """An aware datetime on the test day."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return TZ.localize(datetime(*(int(p) for p in day.split("-")), hour, minute))


def span(start, end, day=DAY, **extra):
    """A timed event between two HH:MM points."""
    event = timed(at(start, day).isoformat())
    event["end"] = at(end, day).isoformat()
    event.update(extra)
    return event


class TestDayIsSpent:

    def spent(self, calendar, events, now="18:00", settings=None):
        merged = dict(GRID, **(settings or {}))
        grid_start, grid_end = calendar.day_bounds(at(now).date(), merged, TZ)
        return calendar.day_is_spent(events, at(now), grid_start, grid_end, merged, TZ)

    @pytest.mark.parametrize(
        "events,now,expected",
        [
            ([], "18:00", True),                         # nothing at all
            ([span("10:00", "11:00")], "18:00", True),   # long finished
            ([span("19:00", "20:00")], "18:00", False),  # still ahead
            ([span("17:00", "19:00")], "18:00", False),  # under way right now
            ([span("17:00", "18:00")], "18:00", True),   # ended exactly now
            # An event outside the rendered hours cannot be seen, so counting it
            # would pin an empty grid to the panel all evening.
            ([span("21:00", "22:00")], "18:00", True),   # after the grid ends
            ([span("08:00", "09:00")], "18:00", True),   # before it starts
            # Only the visible part counts: this one runs to 23:00, the grid to 20:00.
            ([span("19:00", "23:00")], "19:30", False),
            ([span("19:00", "23:00")], "20:01", True),
        ],
    )
    def test_visible_portion_decides(self, calendar, events, now, expected):
        assert self.spent(calendar, events, now) is expected

    def test_zero_length_event_does_not_pin_the_day(self, calendar):
        # Neither DTEND nor DURATION is zero duration per RFC 5545; inventing a
        # default length would hold the day open for time it does not occupy.
        reminder = timed(at("17:00").isoformat())
        assert self.spent(calendar, [reminder], "16:00") is False
        assert self.spent(calendar, [reminder], "17:01") is True

    def test_other_days_are_ignored(self, calendar):
        # The handover fetch is wider than a day, so tomorrow must not read as
        # something still to come today.
        assert self.spent(calendar, [span("09:00", "10:00", day="2026-09-09")]) is True

    def test_overnight_event_counts_only_while_it_runs(self, calendar):
        overnight = timed(at("23:00", "2026-09-07").isoformat())
        overnight["end"] = at("11:00").isoformat()
        assert self.spent(calendar, [overnight], "10:30") is False
        assert self.spent(calendar, [overnight], "11:30") is True

    @pytest.mark.parametrize(
        "settings,expected",
        [
            ({}, True),                                  # all-day ignored by default
            ({"dayOverIncludeAllDay": "true"}, False),   # ...until asked for
        ],
    )
    def test_all_day_events_only_count_when_asked(self, calendar, settings, expected):
        assert self.spent(calendar, [all_day()], settings=settings) is expected

    def test_included_all_day_event_holds_the_whole_day(self, calendar):
        # An all-day event has no end within the day, so when it counts at all it
        # counts until midnight.
        assert self.spent(
            calendar, [all_day()], "23:30", {"dayOverIncludeAllDay": "true"}) is False

    def test_multi_day_all_day_event_covers_the_middle(self, calendar):
        vacation = all_day("vacation", start="2026-09-07", end="2026-09-12")
        assert self.spent(
            calendar, [vacation], settings={"dayOverIncludeAllDay": "true"}) is False

    @pytest.mark.parametrize(
        "settings,expected",
        [
            ({}, True),                                      # free time discounted
            ({"dayOverIgnoreTransparent": "false"}, False),  # ...unless it is not
        ],
    )
    def test_transparent_events_are_discounted_by_default(self, calendar, settings, expected):
        free = span("19:00", "20:00", transparent=True)
        assert self.spent(calendar, [free], settings=settings) is expected

    def test_cancelled_events_never_count(self, calendar):
        # A cancelled event left in the feed would otherwise pin the day forever,
        # and unlike transparency that is not a matter of taste.
        dropped = span("19:00", "20:00", cancelled=True)
        assert self.spent(calendar, [dropped]) is True
        assert self.spent(
            calendar, [dropped], settings={"dayOverIgnoreTransparent": "false"}) is True

    def test_dst_transition_day(self, calendar):
        # Europe/Moscow has no DST, so use a zone that does: on this day two events
        # carry different offsets, which is why the strings cannot be compared as
        # text and the bounds cannot be built by adding to an aware midnight.
        tz = pytz.timezone("Europe/Berlin")
        early = tz.localize(datetime(2026, 10, 25, 1, 30)).isoformat()
        late = tz.localize(datetime(2026, 10, 25, 18, 0)).isoformat()
        assert early[-6:] != late[-6:]        # the offsets really do differ

        event = timed(late)
        event["end"] = tz.localize(datetime(2026, 10, 25, 19, 0)).isoformat()
        grid_start, grid_end = calendar.day_bounds(datetime(2026, 10, 25).date(), GRID, tz)
        now = tz.localize(datetime(2026, 10, 25, 17, 0))
        assert calendar.day_is_spent([event], now, grid_start, grid_end, GRID, tz) is False


class TestDayBounds:

    def test_hours_come_from_the_grid_settings(self, calendar):
        start, end = calendar.day_bounds(at("12:00").date(), GRID, TZ)
        assert (start, end) == (at("10:00"), at("20:00"))

    def test_end_hour_may_be_midnight(self, calendar):
        # "24" cannot be expressed with replace(hour=...), which is why the bounds
        # are built by adding to midnight instead.
        start, end = calendar.day_bounds(
            at("12:00").date(), {"startTimeInterval": "0", "endTimeInterval": "24"}, TZ)
        assert start == at("00:00")
        assert end == TZ.localize(datetime(2026, 9, 9))


class TestResolveDayOverView:

    BASE = dict(GRID, dayOverView="timeGridWeek")

    def resolve(self, calendar, settings=None, events=None, now="18:00",
                view="timeGridDay", offset=0, failed=None):
        return calendar.resolve_day_over_view(
            view, events if events is not None else [], at(now),
            dict(self.BASE, **(settings or {})), TZ, offset, failed or [])

    def test_switches_once_the_day_is_spent(self, calendar):
        assert self.resolve(calendar) == "timeGridWeek"

    def test_stays_put_while_something_remains(self, calendar):
        assert self.resolve(calendar, events=[span("19:00", "20:00")]) is None

    def test_multi_week_is_also_offered(self, calendar):
        assert self.resolve(calendar, {"dayOverView": "dayGrid"}) == "dayGrid"

    @pytest.mark.parametrize("value", ["", None, "listMonth", "timeGridDay", "nonsense"])
    def test_only_known_targets_switch(self, calendar, value):
        # An unknown view would reach get_view_range and fail there, so anything
        # outside the allowlist -- a value left behind by an older form included --
        # means the handover is simply off.
        assert self.resolve(calendar, {"dayOverView": value}) is None

    def test_only_the_day_view_hands_over(self, calendar):
        assert self.resolve(calendar, view="timeGridWeek") is None

    def test_an_offset_day_never_hands_over(self, calendar):
        # "Nothing left today" says nothing about a day rendered at an offset,
        # where every event is wholly ahead or wholly behind.
        assert self.resolve(calendar, offset=1) is None

    def test_a_calendar_that_dropped_out_blocks_the_switch(self, calendar):
        # A failed source reads exactly like an empty one, and acting on it would
        # flip the panel to the week and back as the host recovers -- a 40s
        # repaint each way.
        failed = [{"host": "work.example", "busy": True}]
        assert self.resolve(calendar, failed=failed) is None

    def test_a_calendar_marked_free_does_not_block(self, calendar):
        # Its events never hold the day open, so its absence cannot change the
        # answer; blocking on it would strand the day view for nothing.
        failed = [{"host": "vacation.example", "busy": False}]
        assert self.resolve(calendar, failed=failed) == "timeGridWeek"

    @pytest.mark.parametrize(
        "now,expected",
        [
            ("15:59", None),            # held back
            ("16:00", "timeGridWeek"),  # exactly at the floor
            ("17:30", "timeGridWeek"),
        ],
    )
    def test_earliest_time_holds_the_switch_back(self, calendar, now, expected):
        settings = {"dayOverNotBefore": "true", "dayOverNotBeforeTime": "16:00"}
        assert self.resolve(calendar, settings, now=now) == expected

    def test_earliest_time_is_ignored_when_the_toggle_is_off(self, calendar):
        settings = {"dayOverNotBefore": "false", "dayOverNotBeforeTime": "16:00"}
        assert self.resolve(calendar, settings, now="11:00") == "timeGridWeek"

    @pytest.mark.parametrize("value", ["", "nonsense", "25:00", "12:xx"])
    def test_an_unusable_earliest_time_does_not_block(self, calendar, value):
        settings = {"dayOverNotBefore": "true", "dayOverNotBeforeTime": value}
        assert self.resolve(calendar, settings, now="11:00") == "timeGridWeek"


class TestEventTransparency:

    @pytest.mark.parametrize(
        "transp,busy,expected",
        [
            (None, True, False),          # absent means OPAQUE, per RFC 5545
            ("OPAQUE", True, False),
            ("TRANSPARENT", True, True),
            ("transparent", True, True),  # the case is not guaranteed
            # A calendar marked not-busy overrides its events, which is how a
            # vacation calendar of individually OPAQUE entries is discounted.
            ("OPAQUE", False, True),
            (None, False, True),
        ],
    )
    def test_transparency_sources(self, calendar, transp, busy, expected):
        event = {"transp": icalendar.prop.vText(transp)} if transp else {}
        assert calendar.is_transparent(event, {"busy": busy}) is expected

    def test_busy_defaults_to_true_for_older_instances(self, calendar):
        assert calendar.is_transparent({}, {}) is False


class TestCalendarBusySetting:

    def test_busy_is_zipped_per_calendar(self, calendar):
        sources = calendar.get_calendar_sources({
            "calendarURLs[]": ["https://a.example", "https://b.example"],
            "calendarBusy[]": ["yes", "no"],
        })
        assert [s["busy"] for s in sources] == [True, False]

    def test_missing_busy_array_defaults_to_busy(self, calendar):
        # Instances saved before this setting existed must keep working.
        sources = calendar.get_calendar_sources({
            "calendarURLs[]": ["https://a.example", "https://b.example"]})
        assert [s["busy"] for s in sources] == [True, True]


class FakeDeviceConfig:
    def get_resolution(self):
        return (800, 480)

    def get_config(self, key, default=None):
        return {"timezone": "Europe/Moscow", "time_format": "24h"}.get(key, default)


class TestHandoverWiring:
    """generate_image's ordering: the switch has to land before the view is remapped
    and before the all-day row is laid out for it."""

    SETTINGS = dict(
        GRID, viewMode="timeGridDay", calendarURLs=["https://a.example"],
        dayOverView="timeGridWeek", nowIndicatorInterval="60",
    )

    def run(self, calendar, monkeypatch, events, settings=None):
        merged = dict(self.SETTINGS, **(settings or {}))
        merged["calendarURLs[]"] = merged.pop("calendarURLs")
        fetched, captured = [], {}

        def fetch(sources, tz, start, end, s):
            fetched.append((start, end))
            return list(events), []

        monkeypatch.setattr(calendar, "fetch_ics_events", fetch)
        monkeypatch.setattr(calendar, "render_image",
                            lambda dims, html, css, params: captured.update(params) or "image")
        calendar.generate_image(merged, FakeDeviceConfig())
        return fetched, captured

    def test_a_spent_day_hands_over_and_refetches_the_wider_window(self, calendar, monkeypatch):
        past = span("10:00", "11:00", day=datetime.now(TZ).date().isoformat())
        fetched, params = self.run(calendar, monkeypatch, [past])

        # timeGridWeek is remapped to the rolling timeGrid, which only happens if the
        # switch landed before that step.
        assert params["view"] == "timeGrid"
        assert len(fetched) == 2                       # day window, then the week
        assert (fetched[1][1] - fetched[1][0]).days == 7

    def test_a_day_with_something_left_fetches_once(self, calendar, monkeypatch):
        today = datetime.now(TZ).date().isoformat()
        ahead = span("10:00", "23:59", day=today)
        fetched, params = self.run(calendar, monkeypatch, [ahead])

        assert params["view"] == "timeGridDay"
        assert len(fetched) == 1
        assert (fetched[0][1] - fetched[0][0]).days == 1

    def test_handover_off_never_refetches(self, calendar, monkeypatch):
        fetched, params = self.run(calendar, monkeypatch, [], {"dayOverView": ""})
        assert params["view"] == "timeGridDay"
        assert len(fetched) == 1

    def test_a_failed_second_fetch_falls_back_to_the_day(self, calendar, monkeypatch):
        # The day's events are already in hand, so a wider window that cannot be
        # fetched must not cost the panel its render.
        merged = dict(self.SETTINGS)
        merged["calendarURLs[]"] = merged.pop("calendarURLs")
        calls, captured = [], {}

        def fetch(sources, tz, start, end, s):
            calls.append((start, end))
            if len(calls) > 1:
                raise RuntimeError("No calendar could be reached")
            return [], []

        monkeypatch.setattr(calendar, "fetch_ics_events", fetch)
        monkeypatch.setattr(calendar, "render_image",
                            lambda dims, html, css, params: captured.update(params) or "image")
        assert calendar.generate_image(merged, FakeDeviceConfig()) == "image"
        assert captured["view"] == "timeGridDay"
        # initial_date was rolled back to the day view's, not left on the week's.
        assert captured["initial_date"] == datetime.now(TZ).date().isoformat()

    def test_multi_week_handover_uses_the_grid_span(self, calendar, monkeypatch):
        fetched, params = self.run(
            calendar, monkeypatch, [],
            {"dayOverView": "dayGrid", "displayPastWeeks": "0", "displayWeeks": "3"})
        assert params["view"] == "dayGrid"
        assert params["day_grid_weeks"] == 4
        assert (fetched[1][1] - fetched[1][0]).days == 4 * 7


class TestEventTimeOverride:

    @pytest.mark.parametrize(
        "day_setting,override,handed_over,expected",
        [
            # Without a handover the override is irrelevant, whatever it says.
            ("true", "", False, True),
            ("false", "", False, False),
            ("false", "true", False, False),
            ("true", "false", False, True),
            # After a handover it decides, and an empty value means "same as above".
            ("false", "true", True, True),    # no time on the day, time on the grid
            ("true", "false", True, False),
            ("true", "", True, True),
            ("false", "", True, False),
            # Anything unrecognised falls back rather than reading as false.
            ("true", "nonsense", True, True),
            ("true", None, True, True),
        ],
    )
    def test_override_only_applies_after_a_handover(
            self, calendar, day_setting, override, handed_over, expected):
        settings = {"displayEventTime": day_setting, "dayOverEventTime": override}
        assert calendar.show_event_time(settings, handed_over) is expected

    def test_reaches_the_template(self, calendar, monkeypatch):
        # The one place it is consumed is the template parameter, so a handover has
        # to carry the override all the way there.
        wiring = TestHandoverWiring()
        _, params = wiring.run(
            calendar, monkeypatch, [],
            {"dayOverView": "dayGrid", "displayEventTime": "false",
             "dayOverEventTime": "true"})
        assert params["view"] == "dayGrid"
        assert params["display_event_time"] is True
