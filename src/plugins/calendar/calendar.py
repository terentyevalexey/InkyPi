import os
from utils.app_utils import resolve_path, get_font
from plugins.base_plugin.base_plugin import BasePlugin
from plugins.calendar.constants import LOCALE_MAP, FONT_SIZES
from PIL import Image, ImageColor, ImageDraw, ImageFont
import icalendar
import recurring_ical_events
from io import BytesIO
import logging
import requests
from collections import defaultdict
from datetime import datetime, date, timedelta
import pytz

# Views that render a separate all-day row above a time grid.
TIME_GRID_VIEWS = ("timeGridDay", "timeGridWeek", "timeGrid")

# The all-day row is capped at two lines so it can never squeeze the time grid.
ALL_DAY_MAX_LINES = 2

logger = logging.getLogger(__name__)

class Calendar(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['style_settings'] = True
        template_params['locale_map'] = LOCALE_MAP
        return template_params

    def generate_image(self, settings, device_config):
        calendar_urls = settings.get('calendarURLs[]')
        calendar_colors = settings.get('calendarColors[]')
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

        start, end = self.get_view_range(view, current_dt, settings)
        
        # Fetch events using settings for auth and color logic
        logger.debug(f"Fetching events for {start} --> [{current_dt}] --> {end}")
        events = self.fetch_ics_events(calendar_urls, calendar_colors, tz, start, end, settings)
        
        if not events:
            logger.warning("No events found for provided iCal URLs")

        if view == 'timeGridWeek' and settings.get("displayPreviousDays") != "true":
            view = 'timeGrid'

        all_day_lines, all_day_per_line = 0, self.get_all_day_per_line(settings, view)
        if view in TIME_GRID_VIEWS:
            events, all_day_lines = self.layout_all_day_events(events, settings, view)

        template_params = {
            "view": view,
            "events": events,
            "current_dt": display_now.isoformat(),
            "timezone": timezone,
            "plugin_settings": settings,
            "time_format": time_format,
            "font_scale": FONT_SIZES.get(settings.get("fontSize", "normal"), 1.0),
            "all_day_lines": all_day_lines,
            "all_day_per_line": all_day_per_line
        }

        image = self.render_image(dimensions, "calendar.html", "calendar.css", template_params)

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image
    
    def fetch_ics_events(self, calendar_urls, colors, tz, start_range, end_range, settings):
        parsed_events = []
        
        # Get Attendee settings
        use_attendee_color = settings.get('useAttendeeColor') == 'true'
        attendee_username = settings.get('loginUsername', '').lower()
        attendance_color = settings.get('attendeeColor', '#00FF00')
        contrast_attendance_color = self.get_contrast_color(attendance_color)

        # Colors defined by the calendar itself, opt-in via toggles.
        use_ics_colors = settings.get('useIcsColors') == 'true'
        all_day_color = settings.get('allDayColor') if settings.get('useAllDayColor') == 'true' else None

        for calendar_url, color in zip(calendar_urls, colors):
            cal = self.fetch_calendar(calendar_url, settings)
            events = recurring_ical_events.of(cal).between(start_range, end_range)
            contrast_color = self.get_contrast_color(color)
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

        return parsed_events
    
    def get_all_day_per_line(self, settings, view=None):
        """
        How many all-day events fit on one line of the row. Week views give each day
        its own narrow column, so events there stack one per line no matter what the
        setting says; only the single-column day view can pack them side by side.
        """
        if view in ("timeGridWeek", "timeGrid"):
            return 1
        try:
            return max(1, int(settings.get("allDayMaxPerLine") or 5))
        except (TypeError, ValueError):
            return 5

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

        Returns the events to render plus the number of lines the row needs: 0 drops
        the row entirely, 1 packs everything onto a single line, 2 splits it. Anything
        that still does not fit is replaced by one "+N" chip on the day that overflowed.
        """
        all_day = [e for e in events if e["allDay"]]
        if not all_day:
            return events, 0

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
            return events, lines

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
        return kept, lines

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

    def get_view_range(self, view, current_dt, settings):
        start = datetime(current_dt.year, current_dt.month, current_dt.day)
        if view == "timeGridDay":
            end = start + timedelta(days=1)
        elif view == "timeGridWeek":
            if settings.get("displayPreviousDays") == "true":
                week_start_day = int(settings.get("weekStartDay", 1))
                python_week_start = (week_start_day - 1) % 7
                offset = (current_dt.weekday() - python_week_start) % 7
                start = current_dt - timedelta(days=offset)
                start = datetime(start.year, start.month, start.day)
            end = start + timedelta(days=7)
        elif view == "dayGrid":
            start = current_dt - timedelta(weeks=1)
            end = current_dt + timedelta(weeks=int(settings.get("displayWeeks") or 4))
        elif view == "dayGridMonth":
            start = datetime(current_dt.year, current_dt.month, 1) - timedelta(weeks=1)
            end = datetime(current_dt.year, current_dt.month, 1) + timedelta(weeks=6)
        elif view == "listMonth":
            end = start + timedelta(weeks=5)
        return start, end
        
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

    def fetch_calendar(self, calendar_url, settings):
        username = settings.get("loginUsername")
        password = settings.get("loginPassword")
        
        auth = None
        if username and password:
            auth = (username, password)

        # workaround for webcal urls
        if calendar_url.startswith("webcal://"):
            calendar_url = calendar_url.replace("webcal://", "https://")
        try:
            response = requests.get(calendar_url, auth=auth, timeout=30)
            response.raise_for_status()
            return icalendar.Calendar.from_ical(response.text)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch iCalendar url: {str(e)}")

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
