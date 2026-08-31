"""Tests for calendar recurrence handling.

Two failure modes are pinned here.

``_normalize_recurrence`` turns whatever the caller passed into the list of
RFC5545 lines the Calendar API wants, and drops a malformed line rather than
failing the request.

The second is worse and quieter. ``modify_event`` sends a *full* event body
through ``events().update()``, so a field missing from that body is erased.
Before ``recurrence`` was carried across, changing only the location of a
weekly series dropped its RRULE and left a single event -- the API reports
success, and nobody notices until a meeting is missing from the calendar.
"""

import pytest

from gcalendar import calendar_tools
from gcalendar.calendar_tools import _normalize_recurrence, _preserve_existing_fields

RRULE = "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261210T235959Z"
EXDATE = "EXDATE;TZID=America/Indiana/Indianapolis:20261124T111000,20261126T111000"


# modify_event is registered as a FastMCP tool and wrapped by the auth and
# error-handling decorators. Unwrap to the function itself, which takes the
# Google service as its first argument so a fake can be supplied.
def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


modify_event_fn = _unwrap(calendar_tools.modify_event.fn)


@pytest.mark.parametrize(
    "supplied",
    [
        RRULE,  # a bare string
        [RRULE],  # already a list
        f"  {RRULE}  ",  # stray whitespace
    ],
)
def test_single_rule_normalizes_to_one_line(supplied):
    assert _normalize_recurrence(supplied, "test") == [RRULE]


@pytest.mark.parametrize(
    "supplied",
    [
        [RRULE, EXDATE],
        f"{RRULE}\n{EXDATE}",  # newline separated
        f"{RRULE},{EXDATE}",  # comma separated, as one string
    ],
)
def test_rule_and_exdate_survive_together(supplied):
    """The EXDATE carries the Thanksgiving skip; losing it silently re-adds
    meetings that were deliberately excluded."""
    assert _normalize_recurrence(supplied, "test") == [RRULE, EXDATE]


def test_exdate_commas_are_not_split_into_separate_lines():
    """EXDATE takes a comma-separated date list, so a naive comma split would
    shred one valid line into fragments that no longer parse."""
    assert _normalize_recurrence(EXDATE, "test") == [EXDATE]


@pytest.mark.parametrize("supplied", [None, "", "   ", []])
def test_absent_recurrence_yields_none(supplied):
    assert _normalize_recurrence(supplied, "test") is None


@pytest.mark.parametrize(
    "supplied",
    [
        "every tuesday",  # prose, not RFC5545
        "FREQ=WEEKLY;BYDAY=TU",  # rule body without the RRULE: prefix
        ["RRULE:FREQ=WEEKLY", 42],  # a non-string entry
    ],
)
def test_malformed_input_is_dropped_not_raised(supplied):
    """A bad line is skipped with a warning; the request still goes out."""
    result = _normalize_recurrence(supplied, "test")
    assert result is None or all(
        line.upper().startswith(("RRULE:", "EXDATE", "RDATE", "EXRULE:"))
        for line in result
    )


def test_property_names_match_case_insensitively():
    assert _normalize_recurrence("rrule:FREQ=WEEKLY;BYDAY=TU", "test") == [
        "rrule:FREQ=WEEKLY;BYDAY=TU"
    ]


# --- modify_event's full-body update -------------------------------------


def existing_series():
    return {
        "summary": "CSCI-B 490/B629: Agentic Programming",
        "description": "course page",
        "location": "SPEA/PV A205",
        "recurrence": [RRULE, EXDATE],
        "start": {"dateTime": "2026-09-01T11:10:00-04:00"},
        "end": {"dateTime": "2026-09-01T12:25:00-04:00"},
    }


class FakeEvents:
    """Records the body modify_event sends to events().update()."""

    def __init__(self, existing):
        self._existing = existing
        self.sent_body = None

    def get(self, calendarId, eventId):
        return _Executable(dict(self._existing))

    def update(self, calendarId, eventId, body, conferenceDataVersion=None):
        self.sent_body = body
        return _Executable({**body, "id": eventId, "htmlLink": "http://example.test"})


class _Executable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeService:
    def __init__(self, existing):
        self._events = FakeEvents(existing)

    def events(self):
        return self._events


async def run_modify(existing, **kwargs):
    """Drive the real modify_event and return the body it would PUT."""
    service = FakeService(existing)
    await modify_event_fn(
        service,
        user_google_email="someone@example.test",
        event_id="evt123",
        **kwargs,
    )
    return service._events.sent_body


@pytest.mark.asyncio
async def test_modify_event_location_edit_preserves_the_whole_series():
    """The actual regression, exercised through modify_event rather than
    through the helper: the field mapping at the call site is where the
    recurrence used to be omitted."""
    body = await run_modify(
        existing_series(),
        location="SPEA/PV A205, 1315 E 10th St, Bloomington, IN 47405",
    )

    assert body["location"] == "SPEA/PV A205, 1315 E 10th St, Bloomington, IN 47405"
    assert body["recurrence"] == [RRULE, EXDATE], (
        "modify_event dropped the recurrence: this update replaces the whole "
        "event, so the series would collapse to a single meeting"
    )
    assert body["start"] == {"dateTime": "2026-09-01T11:10:00-04:00"}
    assert body["end"] == {"dateTime": "2026-09-01T12:25:00-04:00"}
    assert body["summary"] == "CSCI-B 490/B629: Agentic Programming"


@pytest.mark.asyncio
async def test_modify_event_can_replace_the_recurrence_rule():
    body = await run_modify(existing_series(), recurrence="RRULE:FREQ=WEEKLY;BYDAY=MO")
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]


@pytest.mark.asyncio
async def test_modify_event_leaves_a_single_event_non_recurring():
    existing = {
        "summary": "one-off",
        "start": {"dateTime": "2026-09-01T11:10:00-04:00"},
        "end": {"dateTime": "2026-09-01T12:25:00-04:00"},
    }
    body = await run_modify(existing, location="somewhere")
    assert "recurrence" not in body


def test_location_only_edit_keeps_the_series_intact():
    """The regression that motivated the fix: changing one field must not
    collapse a recurring event into a single meeting."""
    existing = existing_series()
    new_location = "SPEA/PV A205, 1315 E 10th St, Bloomington, IN 47405"
    body = {"location": new_location}

    _preserve_existing_fields(
        body,
        existing,
        {
            "summary": None,
            "description": None,
            "location": new_location,
            "colorId": body.get("colorId"),
            "recurrence": body.get("recurrence"),
            "start": body.get("start"),
            "end": body.get("end"),
        },
    )

    assert body["location"] == new_location
    assert body["recurrence"] == existing["recurrence"]
    # start/end absent from an update body is a hard API error ("Missing end
    # time"), unlike the silent recurrence loss.
    assert body["start"] == existing["start"]
    assert body["end"] == existing["end"]


def test_explicit_recurrence_overrides_the_existing_rule():
    """Preserving must not make the field read-only."""
    existing = existing_series()
    replacement = ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
    body = {"recurrence": replacement}

    _preserve_existing_fields(
        body,
        existing,
        {
            "summary": None,
            "description": None,
            "location": None,
            "colorId": None,
            "recurrence": body.get("recurrence"),
            "start": body.get("start"),
            "end": body.get("end"),
        },
    )

    assert body["recurrence"] == replacement


def test_single_event_gains_no_recurrence_key():
    """A non-recurring event has no recurrence to carry over, and inventing an
    empty one would turn it into a degenerate series."""
    existing = {
        "summary": "one-off",
        "start": {"dateTime": "2026-09-01T11:10:00-04:00"},
        "end": {"dateTime": "2026-09-01T12:25:00-04:00"},
    }
    body = {"location": "somewhere"}

    _preserve_existing_fields(
        body,
        existing,
        {
            "summary": None,
            "location": "somewhere",
            "recurrence": body.get("recurrence"),
            "start": body.get("start"),
            "end": body.get("end"),
        },
    )

    assert "recurrence" not in body
