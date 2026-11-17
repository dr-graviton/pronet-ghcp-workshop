import asyncio
import base64
import logging
import os
import shutil
import warnings
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Annotated, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from copilot import CopilotClient, JsonRpcError, RuntimeConnection
from copilot.rpc import PermissionDecisionReject
from copilot.session import CustomAgentConfig
from copilot.session_events import (
    AssistantMessageData,
    SessionErrorData,
    SessionEvent,
    SessionIdleData,
    UserMessageData,
)
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from .calendar_cli import DATA_DIR, SCOPES, api_request, required_string

ROOT = Path(__file__).resolve().parent.parent
AGENT_PATH = ROOT / ".github" / "agents" / "FlierReaderAgent.agent.md"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
CALENDAR_TIMEZONE = "America/New_York"
CALENDAR_LOCK = Lock()
logger = logging.getLogger(__name__)

ClockTime = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IslamicDate(ExtractionModel):
    Day: int | None
    Month: str | None
    Year: int | None
    RawText: str | None


class AgendaItem(ExtractionModel):
    Activity: str | None
    ParticipantNames: list[str]
    Time: ClockTime | None
    TimeRawText: str | None


class Event(ExtractionModel):
    EventType: Literal["Majlis", "Jashan", "Dua", "Other"] | None
    EventTypeText: str | None
    SponsorNames: list[str]
    GregorianDate: date | None
    DateRawText: str | None
    StartTime: ClockTime | None
    EndTime: ClockTime | None
    TimeRawText: str | None
    IslamicDate: IslamicDate
    Agenda: list[AgendaItem]
    Warnings: list[str]


class FlierData(ExtractionModel):
    Events: list[Event]
    Warnings: list[str]


class CalendarEventInput(Event):
    @field_validator("GregorianDate", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> object:
        # FastAPI validates decoded JSON rather than using Pydantic's JSON date parsing.
        if isinstance(value, str):
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError("Use a Gregorian date in YYYY-MM-DD format.")
            return parsed
        return value


class CalendarEventResult(ExtractionModel):
    id: str
    calendarId: str
    htmlLink: str


def calendar_datetime(event_date: date, clock_time: str) -> datetime:
    value = datetime.combine(event_date, time.fromisoformat(clock_time), ZoneInfo(CALENDAR_TIMEZONE))
    round_trip = value.astimezone(timezone.utc).astimezone(value.tzinfo)
    if round_trip.replace(tzinfo=None) != value.replace(tzinfo=None):
        raise HTTPException(422, "The event time does not exist due to a daylight-saving change.")
    if value.replace(fold=1).utcoffset() != value.utcoffset():
        raise HTTPException(422, "The event time is ambiguous due to a daylight-saving change.")
    return value


def calendar_event_body(event: Event) -> dict[str, JsonValue]:
    if event.GregorianDate is None or event.StartTime is None:
        raise HTTPException(422, "A Gregorian date and start time are required to create an event.")
    start = calendar_datetime(event.GregorianDate, event.StartTime)
    description = "Reviewed flier details:\n" + event.model_dump_json(indent=2)
    if event.EndTime is None:
        end = (start.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(start.tzinfo)
        description += "\nNo end time was printed; a one-hour duration was used."
    else:
        end_date = event.GregorianDate
        if event.EndTime < event.StartTime:
            end_date += timedelta(days=1)
        end = calendar_datetime(end_date, event.EndTime)
        if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
            raise HTTPException(422, "The event end time must be after its start time.")
    return {
        "summary": event.EventTypeText or event.EventType or "Community event",
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": CALENDAR_TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": CALENDAR_TIMEZONE},
    }


def load_agent() -> CustomAgentConfig:
    definition = AGENT_PATH.read_text(encoding="utf-8")
    header, separator, prompt = definition.partition("\n---\n")
    if not header.startswith("---\n") or not separator or not prompt.strip():
        raise RuntimeError(f"Invalid agent definition: {AGENT_PATH}")
    return {
        "name": "FlierReaderAgent",
        "description": "Extract structured event information from flier images as JSON.",
        "prompt": prompt.strip(),
        "tools": [],
    }


def validate_image(content: bytes) -> str:
    if not content:
        raise HTTPException(400, "The uploaded image is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Choose an image no larger than 10 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                mime_type = IMAGE_TYPES.get(image.format or "")
                if mime_type is None:
                    raise HTTPException(415, "Only JPEG, PNG, and WebP images are supported.")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "Choose an image with no more than 20 million pixels.")
                image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(413, "The image dimensions are too large.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise HTTPException(400, "The file is not a readable image.") from exc
    return mime_type


async def extract_flier(content: bytes, mime_type: str) -> FlierData:
    agent = load_agent()
    with TemporaryDirectory(prefix="flier-reader-") as storage:
        async with (
            asyncio.timeout(180),
            CopilotClient(
                mode="empty",
                base_directory=storage,
                working_directory=str(ROOT),
                connection=RuntimeConnection.for_stdio(
                    path=os.environ.get("COPILOT_CLI_PATH") or shutil.which("copilot")
                ),
            ) as client,
        ):
            async with await client.create_session(
                model=os.environ.get("COPILOT_MODEL", "gpt-6.1-sol"),
                custom_agents=[agent],
                agent=agent["name"],
                available_tools=[],
                enable_session_store=False,
                on_permission_request=lambda _request, _invocation: PermissionDecisionReject(
                    feedback="This app only reads the attached image; tools are not permitted."
                ),
            ) as session:
                done = asyncio.Event()
                response: AssistantMessageData | None = None
                error: str | None = None
                started = False

                def on_event(event: SessionEvent) -> None:
                    nonlocal response, error, started
                    if event.agent_id:
                        return
                    if isinstance(event.data, UserMessageData):
                        started = True
                    elif isinstance(event.data, AssistantMessageData):
                        started = True
                        response = event.data
                    elif isinstance(event.data, SessionErrorData):
                        error = event.data.message
                        done.set()
                    elif isinstance(event.data, SessionIdleData) and started:
                        if event.data.aborted:
                            error = "Copilot extraction was aborted."
                        done.set()

                unsubscribe = session.on(on_event)
                try:
                    async with asyncio.timeout(120):
                        await session.send(
                            "Extract every event from the attached flier using your required "
                            "JSON structure. Return valid JSON only.",
                            attachments=[{
                                "type": "blob",
                                "data": base64.b64encode(content).decode("ascii"),
                                "mimeType": mime_type,
                                "displayName": "uploaded-flier",
                            }],
                        )
                        await done.wait()
                except TimeoutError:
                    await session.abort()
                    raise
                finally:
                    unsubscribe()
                if error is not None:
                    raise RuntimeError(error)
                if response is None:
                    raise RuntimeError("FlierReaderAgent returned no assistant response.")
                return FlierData.model_validate_json(response.content)


app = FastAPI(title="Community Flier Reader")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("index.html"))


@app.post("/api/calendar/events", response_model=CalendarEventResult, status_code=201)
def create_calendar_event(event: CalendarEventInput) -> CalendarEventResult:
    body = calendar_event_body(event)
    with CALENDAR_LOCK:
        token_file = DATA_DIR / "calendar-token.json"
        try:
            credentials = Credentials.from_authorized_user_file(str(token_file))
            if not credentials.has_scopes(SCOPES):
                raise ValueError("The cached token lacks Calendar access.")
            if not credentials.valid:
                credentials.refresh(Request())
                token_file.write_text(credentials.to_json(), encoding="utf-8")
            if not credentials.token:
                raise ValueError("Google did not provide an access token.")
        except (GoogleAuthError, OSError, ValueError) as exc:
            logger.exception("Google Calendar authorization is unavailable")
            raise HTTPException(
                503,
                "Google Calendar authorization is unavailable. Stop the app and run "
                "the calendar CLI with --reauthorize, then restart the app.",
            ) from exc

        try:
            with httpx.Client(
                base_url="https://www.googleapis.com/calendar/v3/",
                headers={"Authorization": f"Bearer {credentials.token}"},
                timeout=30,
            ) as client:
                calendar_file = DATA_DIR / "community-calendar-id.txt"
                if calendar_file.exists():
                    calendar_id = calendar_file.read_text(encoding="utf-8").strip()
                    if not calendar_id:
                        raise ValueError("The saved Community Calendar ID is empty.")
                else:
                    calendar = api_request(client, "POST", "calendars", {
                        "summary": "Community Calendar",
                        "timeZone": CALENDAR_TIMEZONE,
                    })
                    calendar_id = required_string(calendar, "id")
                    calendar_file.write_text(calendar_id, encoding="utf-8")
                created = api_request(
                    client, "POST", f"calendars/{quote(calendar_id, safe='')}/events", body
                )
                return CalendarEventResult(
                    id=required_string(created, "id"),
                    calendarId=calendar_id,
                    htmlLink=required_string(created, "htmlLink"),
                )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.exception("Google Calendar event creation failed")
            raise HTTPException(
                502,
                "Google Calendar creation failed. Check the server terminal and calendar "
                "before retrying; the event may already have been saved.",
            ) from exc


@app.post("/api/extract", response_model=FlierData)
async def extract(image: UploadFile) -> FlierData:
    try:
        content = await image.read(MAX_UPLOAD_BYTES + 1)
    finally:
        await image.close()
    mime_type = validate_image(content)
    try:
        return await extract_flier(content, mime_type)
    except TimeoutError as exc:
        logger.exception("Copilot flier extraction timed out")
        raise HTTPException(504, "Extraction timed out. Please try again.") from exc
    except ValidationError as exc:
        logger.exception("FlierReaderAgent returned invalid JSON or an invalid event schema")
        raise HTTPException(
            502, "The agent returned invalid event data. Please try again."
        ) from exc
    except (JsonRpcError, RuntimeError, OSError) as exc:
        logger.exception("Copilot flier extraction failed")
        raise HTTPException(
            502,
            "Copilot SDK extraction failed. See the server terminal for the exact error.",
        ) from exc
