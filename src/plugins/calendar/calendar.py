import os
from utils.app_utils import resolve_path, get_font
from plugins.base_plugin.base_plugin import BasePlugin
from plugins.calendar.constants import LOCALE_MAP, FONT_SIZES
from PIL import Image, ImageColor, ImageDraw, ImageFont
import icalendar
import recurring_ical_events
from io import BytesIO
import logging
import time
from utils.http_client import get_http_session
from collections import defaultdict
from urllib.parse import urlsplit
from datetime import datetime, date, timedelta
from xml.etree import ElementTree
import pytz

# Views that render a separate all-day row above a time grid.
TIME_GRID_VIEWS = ("timeGridDay", "timeGridWeek", "timeGrid")

# The all-day row is capped at two lines so it can never squeeze the time grid.
ALL_DAY_MAX_LINES = 2

# Connect and read timeouts per calendar request. The shared session retries three
# times, so an unreachable host costs this much again on every attempt; keep it
# short enough that one sick calendar cannot hold the whole refresh hostage.
CALENDAR_TIMEOUT = (10, 20)

CALDAV_NAMESPACES = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav",
                     "A": "http://apple.com/ns/ical/"}

# Asks a CalDAV collection for only the events overlapping the rendered window.
# Discovery: a provider's "CalDAV address" is usually a server root, and the
# calendars sit below it via current-user-principal -> calendar-home-set.
CALDAV_PRINCIPAL_QUERY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>
"""

CALDAV_HOME_QUERY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-home-set/></D:prop>
</D:propfind>
"""

CALDAV_LIST_QUERY = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:" xmlns:A="http://apple.com/ns/ical/">
  <D:prop><D:resourcetype/><D:displayname/><A:calendar-color/></D:prop>
</D:propfind>
"""

CALDAV_QUERY = """<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
"""

logger = logging.getLogger(__name__)

class Calendar(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['style_settings'] = True
        template_params['locale_map'] = LOCALE_MAP
        return template_params

    def generate_image(self, settings, device_config):
        calendar_urls = settings.get('calendarURLs[]')
        view = settings.get("viewMode")

        if not view:
            raise RuntimeError("View is required")
        elif view not in ["timeGridDay", "timeGridWeek", "dayGrid", "dayGridMonth", "listMonth"]:
            raise RuntimeError("Invalid view")

        if not calendar_urls:
            raise RuntimeError("At least one calendar URL is required")
        for url in calendar_urls:
            if not url.strip():
                raise RuntimeError("Invalid calendar URL")

        sources = self.get_calendar_sources(settings)

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        
        timezone = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        tz = pytz.timezone(timezone)

        current_dt = datetime.now(tz)
        
        # Calculate rounded "Now" indicator time based on interval setting
        interval = int(settings.get("nowIndicatorInterval", 60))
        rounded_minute = (current_dt.minute // interval) * interval
        display_now = current_dt.replace(minute=rounded_minute, second=0, microsecond=0)

        # A non-zero offset renders a different day than today, which is how the
        # end-of-day playlist shows tomorrow before switching to the week ahead.
        day_offset = self.get_setting_int(settings, "dayOffset", 0)
        view_dt = current_dt + timedelta(days=day_offset)

        start, end, initial_date = self.get_view_range(view, view_dt, settings)

        # Fetch events using settings for auth and color logic
        logger.debug(f"Fetching events for {start} --> [{view_dt}] --> {end}")
        fetch_started = time.monotonic()
        events = self.fetch_ics_events(sources, tz, start, end, settings)
        fetch_seconds = time.monotonic() - fetch_started

        if not events:
            logger.warning("No events found for provided iCal URLs")

        if view == 'timeGridWeek' and settings.get("displayPreviousDays") != "true":
            view = 'timeGrid'

        all_day_lines, all_day_columns = 0, 1
        if view in TIME_GRID_VIEWS:
            events, all_day_lines, all_day_columns = self.layout_all_day_events(
                events, settings, view)

        template_params = {
            "view": view,
            "events": events,
            "current_dt": display_now.isoformat(),
            "initial_date": initial_date.isoformat(),
            "timezone": timezone,
            "plugin_settings": settings,
            "time_format": time_format,
            "font_scale": FONT_SIZES.get(settings.get("fontSize", "normal"), 1.0),
            "all_day_lines": all_day_lines,
            "all_day_per_line": all_day_columns,
            "day_grid_weeks": self.get_day_grid_weeks(settings),
            "locale_script": self.get_locale_script(settings),
            # Marking "now" on a day that is not today is meaningless.
            "show_now_indicator": settings.get("displayNowIndicator") == "true" and day_offset == 0
        }

        render_started = time.monotonic()
        image = self.render_image(dimensions, "calendar.html", "calendar.css", template_params)
        logger.info(
            "Calendar timing: fetch %.1fs, render %.1fs, %d events",
            fetch_seconds, time.monotonic() - render_started, len(events)
        )

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image
    
    def fetch_ics_events(self, sources, tz, start_range, end_range, settings):
        parsed_events = []
        
        # Get Attendee settings
        use_attendee_color = settings.get('useAttendeeColor') == 'true'
        attendance_color = settings.get('attendeeColor', '#00FF00')
        contrast_attendance_color = self.get_contrast_color(attendance_color)

        # Colors defined by the calendar itself, opt-in via toggles.
        use_ics_colors = settings.get('useIcsColors') == 'true'
        all_day_color = settings.get('allDayColor') if settings.get('useAllDayColor') == 'true' else None

        failed = []
        for source in sources:
            color = source["color"]
            host = urlsplit(source["url"]).hostname
            started = time.monotonic()
            try:
                cal = self.fetch_calendar(source, tz, start_range, end_range)
                fetched = time.monotonic()
                events = recurring_ical_events.of(cal).between(start_range, end_range)
            except Exception as e:
                # One unreachable calendar used to abort the render and leave the
                # panel with nothing; show the calendars that did answer instead.
                failed.append(host)
                logger.warning("Skipping calendar %s after %.1fs: %s",
                               host, time.monotonic() - started, e)
                continue
            logger.info(
                "  %s %s: download %.1fs, expand %.1fs, %d events",
                "caldav" if source["caldav"] else "ics  ",
                host, fetched - started, time.monotonic() - fetched, len(events)
            )
            contrast_color = self.get_contrast_color(color)
            # Attendance is matched against the login for this calendar, since each
            # calendar now carries its own.
            attendee_username = ((source["auth"][0] if source["auth"] else "") or "").lower()
            calendar_color = None
            if use_ics_colors:
                calendar_color = self.parse_ics_color(cal.get('X-APPLE-CALENDAR-COLOR') or cal.get('COLOR'))

            for event in events:
                start, end, all_day = self.parse_data_points(event, tz)
                
                # Check for attendance if enabled
                is_attending = False
                if use_attendee_color and attendee_username:
                    attendees = event.get('ATTENDEE', [])
                    if isinstance(attendees, icalendar.vCalAddress):
                        attendees = [attendees]
                    organizer = event.get('ORGANIZER')
                    if organizer:
                        attendees.append(organizer)
                    if not isinstance(attendees, list):
                        attendees = [attendees]
                    is_attending = any(attendee_username in str(a).lower() for a in attendees)

                base_color, base_text = color, contrast_color
                if use_ics_colors:
                    ics_color = self.parse_ics_color(event.get('COLOR')) or calendar_color
                    if ics_color:
                        base_color, base_text = ics_color, self.get_contrast_color(ics_color)
                elif all_day and all_day_color:
                    base_color, base_text = all_day_color, self.get_contrast_color(all_day_color)

                current_bg = attendance_color if is_attending else base_color
                current_text = contrast_attendance_color if is_attending else base_text

                parsed_event = {
                    "title": str(event.get("summary")),
                    "start": start,
                    "backgroundColor": current_bg,
                    "textColor": current_text,
                    "allDay": all_day
                }
                if end:
                    parsed_event['end'] = end

                parsed_events.append(parsed_event)

        if failed and len(failed) == len(sources):
            raise RuntimeError(f"No calendar could be reached: {', '.join(failed)}")
        return parsed_events
    
    def get_locale_script(self, settings):
        """
        The locale file to load, or None for English, which the main bundle carries.

        Only one language is ever rendered, so the combined 79-locale bundle would be
        parsed in full to use a single entry. Checked against LOCALE_MAP so the value
        cannot be turned into a path.
        """
        language = settings.get("language")
        if not language or language == "en" or language not in LOCALE_MAP:
            return None
        return f"{language}.global.min.js"

    def get_all_day_per_line(self, settings, view=None):
        """
        How many all-day events fit on one line of the row. Week views give each day
        its own narrow column, so events there stack one per line no matter what the
        setting says; only the single-column day view can pack them side by side.
        """
        if view in ("timeGridWeek", "timeGrid"):
            return 1
        return max(1, self.get_setting_int(settings, "allDayMaxPerLine", 5))

    @staticmethod
    def all_day_span(event):
        """Every date an all-day event covers. DTEND is exclusive, per RFC 5545."""
        start = date.fromisoformat(event["start"][:10])
        end = date.fromisoformat(event["end"][:10]) if event.get("end") else start + timedelta(days=1)
        if end <= start:
            end = start + timedelta(days=1)
        days, day = [], start
        while day < end:
            days.append(day)
            day += timedelta(days=1)
        return days

    def layout_all_day_events(self, events, settings, view=None):
        """
        Cap the all-day row at two lines so it can never squeeze the time grid.

        Returns the events to render, the number of lines the row needs, and how many
        columns to divide each line into. 0 lines drops the row entirely, 1 packs
        everything onto a single line, 2 splits it. Anything that still does not fit is
        replaced by one "+N" chip on the day that overflowed.
        """
        all_day = [e for e in events if e["allDay"]]
        if not all_day:
            return events, 0, 1

        per_line = self.get_all_day_per_line(settings, view)
        ordered = sorted(all_day, key=lambda e: (e["start"], e.get("end") or "", e["title"]))

        by_day = defaultdict(list)
        for event in ordered:
            for day in self.all_day_span(event):
                by_day[day].append(event)

        busiest = max(len(day_events) for day_events in by_day.values())
        lines = 1 if busiest <= per_line else ALL_DAY_MAX_LINES
        capacity = lines * per_line
        if busiest <= capacity:
            return events, lines, self.get_all_day_columns(busiest, lines, per_line)

        # An event survives if it fits on at least one day it covers, so a multi-day
        # event is never chopped in half by a single crowded day.
        visible = set()
        for day_events in by_day.values():
            fitting = day_events[:capacity - 1] if len(day_events) > capacity else day_events
            visible.update(id(event) for event in fitting)

        kept = [e for e in events if not e["allDay"] or id(e) in visible]
        for day, day_events in sorted(by_day.items()):
            hidden = sum(1 for e in day_events if id(e) not in visible)
            if hidden:
                kept.append(self.overflow_chip(day, hidden, settings))

        # Count what actually ends up on screen, chips included, so the columns match.
        rendered = self.count_all_day_per_day(kept)
        return kept, lines, self.get_all_day_columns(rendered, lines, per_line)

    def count_all_day_per_day(self, events):
        """How many all-day entries land on the busiest single day."""
        counts = defaultdict(int)
        for event in events:
            if event["allDay"]:
                for day in self.all_day_span(event):
                    counts[day] += 1
        return max(counts.values()) if counts else 0

    @staticmethod
    def get_all_day_columns(busiest, lines, per_line):
        """
        Divide each line among the events that are actually there, rather than always
        cutting it into per_line slots. A lone all-day event then spans the row the way
        a lone timed event spans its column, and two lines come out balanced: seven
        events read better as 4 + 3 than as 5 + 2.
        """
        if busiest <= 0:
            return 1
        return max(1, min(per_line, -(-busiest // lines)))

    def overflow_chip(self, day, count, settings):
        """A synthetic all-day event standing in for the events that did not fit."""
        color = settings.get("allDayOverflowColor") or "#808080"
        return {
            "title": f"+{count}",
            "start": day.isoformat(),
            "end": (day + timedelta(days=1)).isoformat(),
            "backgroundColor": color,
            "textColor": self.get_contrast_color(color),
            "allDay": True,
            "overflowCount": count
        }

    def get_view_range(self, view, view_dt, settings):
        """
        The window of events to fetch, plus the date the view should open on.

        The two differ: a multi-week grid fetches from the first visible week and
        opens there, while day and month views open on the date being displayed.
        """
        start = datetime(view_dt.year, view_dt.month, view_dt.day)
        initial_date = view_dt.date()

        if view == "timeGridDay":
            end = start + timedelta(days=1)
        elif view == "timeGridWeek":
            if settings.get("displayPreviousDays") == "true":
                start = self.get_week_start(view_dt, settings)
                initial_date = start.date()
            end = start + timedelta(days=7)
        elif view == "dayGrid":
            past, future = self.get_day_grid_span(settings)
            start = self.get_week_start(view_dt, settings) - timedelta(weeks=past)
            end = start + timedelta(weeks=past + 1 + future)
            initial_date = start.date()
        elif view == "dayGridMonth":
            start = datetime(view_dt.year, view_dt.month, 1) - timedelta(weeks=1)
            end = datetime(view_dt.year, view_dt.month, 1) + timedelta(weeks=6)
        elif view == "listMonth":
            end = start + timedelta(weeks=5)
        return start, end, initial_date

    def get_week_start(self, view_dt, settings):
        """Midnight on the first day of the week containing view_dt."""
        # weekStartDay follows FullCalendar's firstDay, where 0 is Sunday; Python
        # counts weekdays from Monday.
        python_week_start = (self.get_setting_int(settings, "weekStartDay", 0) - 1) % 7
        offset = (view_dt.weekday() - python_week_start) % 7
        day = view_dt - timedelta(days=offset)
        return datetime(day.year, day.month, day.day)

    def get_day_grid_span(self, settings):
        """Weeks of history and of future shown either side of the current week."""
        return (self.get_setting_int(settings, "displayPastWeeks", 1),
                self.get_setting_int(settings, "displayWeeks", 3))

    def get_day_grid_weeks(self, settings):
        past, future = self.get_day_grid_span(settings)
        return past + 1 + future

    @staticmethod
    def get_setting_int(settings, key, default):
        try:
            return int(settings.get(key) or default)
        except (TypeError, ValueError):
            return default

    def parse_data_points(self, event, tz):
        all_day = False
        dtstart = event.decoded("dtstart")
        if isinstance(dtstart, datetime):
            start = dtstart.astimezone(tz).isoformat()
        else:
            start = dtstart.isoformat()
            all_day = True

        end = None
        if "dtend" in event:
            dtend = event.decoded("dtend")
            if isinstance(dtend, datetime):
                end = dtend.astimezone(tz).isoformat()
            else:
                end = dtend.isoformat()
        elif "duration" in event:
            duration = event.decoded("duration")
            end = (dtstart + duration).isoformat()
        return start, end, all_day

    def action_discover(self, data):
        """
        Expand a CalDAV server address into the calendars behind it, so the settings
        form can offer them as separate rows to keep or drop.
        """
        url = (data.get("url") or "").strip()
        if not url:
            raise RuntimeError("A calendar URL is required")
        if url.startswith("webcal://"):
            url = url.replace("webcal://", "https://", 1)

        username, password = data.get("username") or "", data.get("password") or ""
        auth = (username, password) if username and password else None

        calendars = self.discover_caldav_calendars(url, auth)
        if not calendars:
            raise RuntimeError(f"No calendars found under {url}")
        return {"calendars": calendars}

    def get_calendar_sources(self, settings):
        """
        Zip the parallel form arrays into one record per calendar.

        Credentials are per calendar because a single shared login is sent to every
        URL, which leaks one provider's password to every other host in the list.
        """
        urls = settings.get('calendarURLs[]') or []
        colors = settings.get('calendarColors[]') or []
        usernames = settings.get('calendarUsernames[]') or []
        passwords = settings.get('calendarPasswords[]') or []
        kinds = settings.get('calendarTypes[]') or []

        # Instances saved before per-calendar credentials existed carry one shared
        # login; keep them working, but say plainly what that costs.
        legacy = (settings.get('loginUsername'), settings.get('loginPassword'))
        share_legacy = all(legacy) and not any(usernames)
        if share_legacy:
            logger.warning(
                "Calendar is sending one shared login to every calendar URL, including "
                "hosts it does not belong to. Re-save the plugin to give each calendar "
                "its own credentials."
            )

        sources = []
        for index, url in enumerate(urls):
            username = usernames[index] if index < len(usernames) else ''
            password = passwords[index] if index < len(passwords) else ''
            if share_legacy:
                username, password = legacy
            sources.append({
                "url": url.strip(),
                "color": colors[index] if index < len(colors) else '#007BFF',
                "auth": (username, password) if username and password else None,
                "caldav": (kinds[index] if index < len(kinds) else 'ics') == 'caldav',
            })
        return sources

    def fetch_calendar(self, source, tz, start_range, end_range):
        url = source["url"]
        # workaround for webcal urls
        if url.startswith("webcal://"):
            url = url.replace("webcal://", "https://", 1)

        if source["auth"] and urlsplit(url).scheme != "https":
            logger.warning(
                "Sending calendar credentials in clear text to %s; use an https URL",
                urlsplit(url).hostname or "the calendar host"
            )

        if source["caldav"]:
            return self.fetch_caldav(url, source["auth"], tz, start_range, end_range)

        try:
            response = get_http_session().get(url, auth=source["auth"], timeout=CALENDAR_TIMEOUT)
            response.raise_for_status()
            return icalendar.Calendar.from_ical(response.text)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch iCalendar url: {str(e)}")

    def fetch_caldav(self, url, auth, tz, start_range, end_range):
        """
        Ask CalDAV for just the window being rendered.

        The ICS export of the same data is the whole calendar — megabytes and
        thousands of events — where a day view needs a handful of them.

        The URL may be a single calendar, or the server address a provider hands out:
        Nextcloud calls /remote.php/dav "the CalDAV address", but a calendar-query
        against it answers 415 because it is not a calendar. So try the URL directly
        first, and fall back to discovering the calendars underneath it.
        """
        query = CALDAV_QUERY.format(
            start=self.caldav_timestamp(start_range, tz),
            end=self.caldav_timestamp(end_range, tz),
        )

        direct = self.caldav_request("REPORT", url, auth, query, depth="1", required=False)
        if direct is not None:
            return self.merge_caldav_response(direct.content)

        collections = self.discover_caldav_calendars(url, auth)
        if not collections:
            raise RuntimeError(
                f"{url} is not a calendar and no calendars were found under it"
            )

        logger.info(
            "Discovered %d calendars under %s: %s",
            len(collections), url, ", ".join(c["name"] for c in collections)
        )
        bodies = [
            self.caldav_request("REPORT", c["url"], auth, query, depth="1").content
            for c in collections
        ]
        return self.merge_caldav_response(bodies)

    def caldav_request(self, method, url, auth, body, depth="0", required=True):
        """
        One DAV request. With required=False a refusal returns None instead of raising,
        which is how an unsuitable URL is told apart from an unreachable server.
        """
        try:
            response = get_http_session().request(
                method, url, data=body.encode("utf-8"), auth=auth,
                timeout=CALENDAR_TIMEOUT,
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            )
        except Exception as e:
            raise RuntimeError(f"CalDAV {method} failed: {str(e)}")

        if response.ok:
            return response
        if not required:
            logger.debug("CalDAV %s on %s answered %s", method, url, response.status_code)
            return None
        raise RuntimeError(
            f"CalDAV {method} on {url} failed: {response.status_code} {response.reason}"
        )

    def discover_caldav_calendars(self, url, auth):
        """
        Walk a CalDAV server address down to the calendars it holds, following
        current-user-principal to calendar-home-set to the collections inside it.

        Returns (display name, url) pairs.
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        principal = self.caldav_href(
            self.caldav_request("PROPFIND", url, auth, CALDAV_PRINCIPAL_QUERY),
            "D:current-user-principal")
        if not principal:
            return []

        home = self.caldav_href(
            self.caldav_request("PROPFIND", origin + principal, auth, CALDAV_HOME_QUERY),
            "C:calendar-home-set")
        if not home:
            return []

        listing = self.caldav_request(
            "PROPFIND", origin + home, auth, CALDAV_LIST_QUERY, depth="1")
        return self.caldav_collections(listing.content, origin, home)

    def caldav_href(self, response, prop):
        """
        The href inside a named property. The prop is given with its prefix because
        the two used here live in different namespaces: current-user-principal is
        DAV, calendar-home-set is CalDAV.
        """
        node = self.parse_caldav_xml(response.content).find(
            f".//{prop}/D:href", CALDAV_NAMESPACES)
        return node.text.strip() if node is not None and node.text else None

    def caldav_collections(self, xml_body, origin, home):
        """
        Calendar collections in a listing, skipping the home collection itself.

        Returns dicts of name, url and the calendar's own colour where the server
        publishes one, so discovered calendars arrive already distinguishable.
        """
        found = []
        for response in self.parse_caldav_xml(xml_body).findall(
                ".//D:response", CALDAV_NAMESPACES):
            href_node = response.find("D:href", CALDAV_NAMESPACES)
            if href_node is None or not href_node.text:
                continue
            href = href_node.text.strip()
            if href.rstrip("/") == home.rstrip("/"):
                continue
            if response.find(".//D:resourcetype/C:calendar", CALDAV_NAMESPACES) is None:
                continue
            name_node = response.find(".//D:displayname", CALDAV_NAMESPACES)
            name = name_node.text if name_node is not None and name_node.text else href
            color_node = response.find(".//A:calendar-color", CALDAV_NAMESPACES)
            color = self.parse_ics_color(color_node.text) if color_node is not None else None
            found.append({"name": name, "url": origin + href, "color": color})
        return found

    @staticmethod
    def caldav_timestamp(dt, tz):
        """CalDAV time-range bounds are UTC in iCalendar's basic format."""
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        return dt.astimezone(pytz.utc).strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def parse_caldav_xml(xml_body):
        """
        ElementTree expands internal entities, so a hostile or compromised server could
        stall the device with a nested-entity bomb. A DAV response never carries a
        doctype, so refusing one costs nothing.
        """
        if b"<!DOCTYPE" in xml_body or b"<!ENTITY" in xml_body:
            raise RuntimeError("CalDAV response declares XML entities; refusing to parse it")
        try:
            return ElementTree.fromstring(xml_body)
        except ElementTree.ParseError as e:
            raise RuntimeError(f"CalDAV server returned malformed XML: {str(e)}")

    def merge_caldav_response(self, xml_bodies):
        """
        Fold every calendar-data blob of one or more multistatus responses into a
        single calendar.

        Each blob is a self-contained VCALENDAR for one resource, so the VTIMEZONE
        definitions repeat and have to be de-duplicated by TZID.
        """
        if isinstance(xml_bodies, (bytes, bytearray, str)):
            xml_bodies = [xml_bodies]

        merged, timezones = None, set()
        for xml_body in xml_bodies:
            root = self.parse_caldav_xml(xml_body)
            for node in root.findall(".//C:calendar-data", CALDAV_NAMESPACES):
                if not (node.text or "").strip():
                    continue
                try:
                    parsed = icalendar.Calendar.from_ical(node.text)
                except ValueError:
                    logger.warning("Skipping unparsable CalDAV resource")
                    continue
                if merged is None:
                    merged = icalendar.Calendar()
                    for key, value in parsed.items():
                        merged[key] = value
                for component in parsed.subcomponents:
                    if component.name == "VTIMEZONE":
                        tzid = str(component.get("TZID"))
                        if tzid in timezones:
                            continue
                        timezones.add(tzid)
                    merged.add_component(component)

        return merged if merged is not None else icalendar.Calendar()

    def parse_ics_color(self, value):
        """
        Normalise a color defined in the calendar to hex. RFC 7986 COLOR carries a CSS3
        name ("palevioletred"), while X-APPLE-CALENDAR-COLOR carries hex; accept both.
        """
        if not value:
            return None
        try:
            r, g, b = ImageColor.getrgb(str(value).strip())[:3]
        except ValueError:
            logger.debug(f"Ignoring unrecognised calendar color: {value}")
            return None
        return f"#{r:02x}{g:02x}{b:02x}"

    def get_contrast_color(self, color):
        """
        Returns '#000000' (black) or '#ffffff' (white) depending on the contrast
        against the given color.
        """
        r, g, b = ImageColor.getrgb(color)
        # YIQ formula to estimate brightness
        yiq = (r * 299 + g * 587 + b * 114) / 1000

        return '#000000' if yiq >= 150 else '#ffffff'
