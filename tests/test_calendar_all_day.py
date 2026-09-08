import os
import sys

import pytest

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
        kept, actual_lines = calendar.layout_all_day_events(events, {})

        chips = [e for e in kept if e.get("overflowCount")]
        shown = [e for e in kept if e["allDay"] and not e.get("overflowCount")]

        assert actual_lines == lines
        assert len(shown) == visible
        assert [c["overflowCount"] for c in chips] == ([overflow] if overflow else [])
        # The chip always accounts for exactly what was dropped.
        assert len(shown) + sum(c["overflowCount"] for c in chips) == count

    def test_timed_events_are_never_dropped(self, calendar):
        events = [all_day(f"e{i}") for i in range(20)] + [timed()]
        kept, _ = calendar.layout_all_day_events(events, {})
        assert sum(1 for e in kept if not e["allDay"]) == 1

    def test_multi_day_event_survives_a_crowded_day(self, calendar):
        trip = all_day("trip", DAY, "2026-09-11")
        events = [trip] + [all_day(f"e{i}") for i in range(15)]
        kept, _ = calendar.layout_all_day_events(events, {})
        assert any(e["title"] == "trip" for e in kept)

    def test_week_views_stack_one_event_per_line(self, calendar):
        # A week gives each day its own narrow column, so five events cannot sit
        # side by side the way they do in the single-column day view.
        events = [all_day(f"e{i}") for i in range(5)]
        kept, lines = calendar.layout_all_day_events(events, {}, "timeGridWeek")
        shown = [e for e in kept if not e.get("overflowCount")]
        chips = [e for e in kept if e.get("overflowCount")]
        assert (lines, len(shown)) == (2, 1)
        assert chips[0]["overflowCount"] == 4

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
