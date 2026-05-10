from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import asyncio
import httpx
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import os
from typing import Dict
from rbh_builder import ApiError, AuthenticationError, RainbowClient, RequestError

load_dotenv()

# -----------------------------------------------------------------------------
# App & Logging
# -----------------------------------------------------------------------------

logger = logging.getLogger("uvicorn.error")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# -----------------------------------------------------------------------------
# PMS CONFIG
# -----------------------------------------------------------------------------

PMS_BASE_URL = os.getenv("PMS_BASE_URL", "https://red-rhg.openrainbow.io/provisioningapi")
PMS_API_BASE_URL = os.getenv("PMS_API_BASE_URL", f"{PMS_BASE_URL.rstrip('/')}/api")
ROOM_SERVICE_URL = os.getenv(
    "ROOM_SERVICE_URL", "https://sgdemo-vna01.apac-deepsky.com:1357/api/flows/new-request"
)
ROOM_SERVICE_VERIFY = os.getenv("ROOM_SERVICE_VERIFY", "true").lower() != "false"
PMS_TIMEOUT = int(os.getenv("PMS_TIMEOUT", "10"))

PMS_USERNAME = os.getenv("PMS_USERNAME")
PMS_PASSWORD = os.getenv("PMS_PASSWORD")

if not PMS_USERNAME or not PMS_PASSWORD:
    raise RuntimeError("PMS credentials not set")

# -----------------------------------------------------------------------------
# UTILITIES
# -----------------------------------------------------------------------------

def get_attr(body: dict, name: str):
    try:
        return body[name]["value"][0]
    except Exception:
        return None


def sanitize_url(url: str | None) -> str:
    if not url:
        return "http://www.al-enterprise.com"
    if "apple.com" in url:
        return "http://www.al-enterprise.com"
    return url

# -----------------------------------------------------------------------------
# PMS / RAINBOW HOSPITALITY CLIENT
# -----------------------------------------------------------------------------

def build_pms_client() -> RainbowClient:
    logger.info("Building Rainbow Hospitality client")
    try:
        return (
            RainbowClient.builder(base_url=PMS_API_BASE_URL, timeout=PMS_TIMEOUT)
            .with_credentials(username=PMS_USERNAME, password=PMS_PASSWORD)
            .build()
        )
    except (ApiError, AuthenticationError, RequestError) as exc:
        logger.error("Rainbow Hospitality login failed: %s", exc)
        raise HTTPException(status_code=503, detail="PMS unavailable") from exc

# -----------------------------------------------------------------------------
# FETCH ROOMS
# -----------------------------------------------------------------------------

def extract_paginated_items(payload: dict) -> list[dict]:
    data = payload.get("Data", payload.get("data", payload))
    if isinstance(data, dict):
        items = data.get("Data", data.get("data", []))
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


async def fetch_rooms(client: RainbowClient | None = None):
    def _fetch() -> list[dict]:
        pms = client or build_pms_client()
        payload = pms.get_rooms(page_number=1, page_size=100)
        return extract_paginated_items(payload)

    try:
        return await asyncio.to_thread(_fetch)
    except (ApiError, AuthenticationError, RequestError) as exc:
        logger.error("PMS rooms fetch failed: %s", exc)
        raise HTTPException(status_code=503, detail="PMS unavailable") from exc


def room_number_matches(room: dict, room_number: str) -> bool:
    return str(room.get("roomNumber") or room.get("RoomNumber") or room.get("roomNo") or room.get("RoomNo")) == str(room_number)


async def find_room_by_number(room_number: str, client: RainbowClient | None = None) -> dict | None:
    rooms = await fetch_rooms(client)
    return next((room for room in rooms if room_number_matches(room, room_number)), None)


def get_room_occupation(room: dict) -> str | None:
    return room.get("occupation") or room.get("Occupation")


def get_room_guest(room: dict) -> dict | None:
    return room.get("guest") or room.get("Guest")


def get_room_checkin(room: dict) -> str:
    return room.get("checkinDate") or room.get("CheckinDate") or room.get("checkInDate") or room.get("CheckInDate")


def get_room_checkout(room: dict) -> str:
    return room.get("checkoutDate") or room.get("CheckoutDate") or room.get("checkOutDate") or room.get("CheckOutDate")


def get_guest_last_name(guest: dict) -> str:
    return guest.get("lastName") or guest.get("LastName") or ""


def guest_is_deleted(guest: dict) -> bool:
    return bool(guest.get("isDeleted") or guest.get("IsDeleted"))


def get_room_id(room: dict) -> str | None:
    return room.get("id") or room.get("Id") or room.get("roomId") or room.get("RoomId")


def normalize_wakeup_frequency(value: str | None) -> str:
    normalized = (value or "Once").strip().lower()
    mappings = {
        "once": "Once",
        "one-time": "Once",
        "one time": "Once",
        "single": "Once",
        "daily": "Daily",
        "every day": "Daily",
        "repeat daily": "Daily",
        "weekly": "Weekly",
        "every week": "Weekly",
        "repeat weekly": "Weekly",
    }
    return mappings.get(normalized, "Once")


def normalize_wakeup_datetime_for_pms(dt_str: str) -> str:
    """
    Convert a hotel-local or timezone-aware ISO datetime to the UTC-naive format
    expected by Rainbow Hospitality wake-up alarms.

    Voicebot requests intentionally send hotel-local wall time without an offset,
    e.g. 2026-05-12T07:00:00. Browser requests may send UTC with a Z suffix.
    RBH stores/displays wake-up alarms as local time after interpreting the API
    value as UTC, so naive local times must be shifted to UTC before submission.
    """
    hotel_timezone = os.getenv("HOTEL_TIMEZONE", "Asia/Singapore")
    pms_time_basis = os.getenv("PMS_WAKEUP_TIME_BASIS", "utc").strip().lower()
    try:
        parsed = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
    except Exception:
        return dt_str

    if pms_time_basis in {"local", "hotel", "no_conversion"}:
        return parsed.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(hotel_timezone))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def parse_iso_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
    except Exception:
        return None


def hotel_local_date(dt_str: str | None) -> str | None:
    parsed = parse_iso_datetime(dt_str)
    if parsed is None:
        return None
    hotel_timezone = ZoneInfo(os.getenv("HOTEL_TIMEZONE", "Asia/Singapore"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=hotel_timezone)
    return parsed.astimezone(hotel_timezone).date().isoformat()


def wakeup_room_number(wakeup: dict) -> str | None:
    room = (
        ((wakeup.get("roomActions") or {}).get("room") or {})
        or ((wakeup.get("RoomActions") or {}).get("Room") or {})
    )
    return str(room.get("roomNumber") or room.get("RoomNumber") or "") or None


def wakeup_id(wakeup: dict) -> str | None:
    value = wakeup.get("id") or wakeup.get("Id")
    return str(value) if value else None


def wakeup_alarm_time(wakeup: dict) -> str | None:
    return wakeup.get("alarmTime") or wakeup.get("AlarmTime") or wakeup.get("fromDate") or wakeup.get("FromDate")


def wakeup_created_time(wakeup: dict) -> datetime:
    return parse_iso_datetime(wakeup.get("createdDate") or wakeup.get("CreatedDate")) or datetime.min.replace(tzinfo=timezone.utc)


def wakeup_is_active(wakeup: dict) -> bool:
    status = str(wakeup.get("status") or wakeup.get("Status") or "").strip().lower()
    return not bool(wakeup.get("isDeleted") or wakeup.get("IsDeleted")) and status not in {"deleted", "cancelled", "canceled", "completed"}


def find_wakeup_calls_for_room_date(pms: RainbowClient, room_number: str, requested_alarm_time: str) -> list[dict]:
    requested_date = hotel_local_date(requested_alarm_time)
    if not requested_date:
        return []
    payload = pms.get_wakeup_calls(page_number=1, page_size=int(os.getenv("PMS_WAKEUP_LOOKUP_PAGE_SIZE", "100")))
    matches = []
    for wakeup in extract_paginated_items(payload):
        if not wakeup_is_active(wakeup):
            continue
        if wakeup_room_number(wakeup) != str(room_number):
            continue
        if hotel_local_date(wakeup_alarm_time(wakeup)) == requested_date:
            matches.append(wakeup)
    matches.sort(key=wakeup_created_time, reverse=True)
    return matches


def delete_extra_wakeup_calls(pms: RainbowClient, wakeups: list[dict], keep_id: str) -> list[dict]:
    if os.getenv("PMS_WAKEUP_DEDUPLICATE_SAME_DAY", "true").strip().lower() != "true":
        return []
    deleted = []
    for wakeup in wakeups:
        current_id = wakeup_id(wakeup)
        if not current_id or current_id == keep_id:
            continue
        try:
            deleted.append({"id": current_id, "result": pms.delete_wakeup_call(current_id)})
        except (ApiError, AuthenticationError, RequestError) as exc:
            logger.warning("Could not delete duplicate wakeup call id=%s: %s", current_id, exc)
            deleted.append({"id": current_id, "error": str(exc)})
    return deleted

# -----------------------------------------------------------------------------
# ROOT
# -----------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok"}

# -----------------------------------------------------------------------------
# STELLAR CAPTIVE PORTAL LOGIN PAGE
# -----------------------------------------------------------------------------

@app.get("/portal/login", response_class=HTMLResponse)
async def stellar_login_get(request: Request):
    qp = request.query_params
    error = qp.get("error")
    error_msg = "User not found or password incorrect" if error == "1" else None

    context = {
        "request": request,
        "ap_login_url": "http://cportal.al-enterprise.com/login",
        "ssid": qp.get("ssid"),
        "switchip": qp.get("switchip"),
        "switchmac": qp.get("switchmac"),
        "clientip": qp.get("clientip"),
        "clientmac": qp.get("clientmac"),
        "url": sanitize_url(qp.get("url")),
        "error_msg": error_msg,
    }

    return templates.TemplateResponse("stellar_login.html", context)

# -----------------------------------------------------------------------------
# RADIUS AUTH
# -----------------------------------------------------------------------------

@app.post("/radius/auth")
async def radius_auth(request: Request):
    body = await request.json()

    room_number = get_attr(body, "User-Name")
    password = get_attr(body, "User-Password")

    logger.info(f"RADIUS AUTH room={room_number}")

    if not room_number or not password:
        raise HTTPException(status_code=401)

    room = await find_room_by_number(room_number)
    if not room:
        raise HTTPException(status_code=401)

    if get_room_occupation(room) != "Occupied":
        raise HTTPException(status_code=401)

    guest = get_room_guest(room)
    if not guest or guest_is_deleted(guest):
        raise HTTPException(status_code=401)

    now = datetime.now(timezone.utc)
    checkin = datetime.fromisoformat(get_room_checkin(room).replace("Z", "+00:00"))
    checkout = datetime.fromisoformat(get_room_checkout(room).replace("Z", "+00:00"))

    if not (checkin <= now <= checkout):
        raise HTTPException(status_code=401)

    if password.lower().strip() != get_guest_last_name(guest).lower():
        raise HTTPException(status_code=401)

    logger.info(f"ACCESS GRANTED room={room_number}")
    return {}

# -----------------------------------------------------------------------------
# GUEST VALIDATION (API)
# -----------------------------------------------------------------------------

async def validate_guest(room_number: str, last_name: str) -> dict:
    logger.info(f"Validating guest room={room_number} last_name={last_name}")

    rooms = await fetch_rooms()

    logger.info(f"PMS returned {len(rooms)} rooms")

    room = next((r for r in rooms if room_number_matches(r, room_number)), None)
    if not room:
        logger.error("Room not found in PMS")
        raise HTTPException(status_code=401)

    logger.info(f"Room found: occupation={get_room_occupation(room)}")

    if get_room_occupation(room) != "Occupied":
        logger.error("Room not occupied")
        raise HTTPException(status_code=401)

    guest = get_room_guest(room)
    logger.info(f"Guest object: {guest}")

    if not guest:
        logger.error("Guest missing")
        raise HTTPException(status_code=401)

    if guest_is_deleted(guest):
        logger.error("Guest marked deleted")
        raise HTTPException(status_code=401)

    now = datetime.now(timezone.utc)
    checkin = datetime.fromisoformat(get_room_checkin(room).replace("Z", "+00:00"))
    checkout = datetime.fromisoformat(get_room_checkout(room).replace("Z", "+00:00"))

    logger.info(f"Checkin={checkin}, Checkout={checkout}, Now={now}")

    if not (checkin <= now <= checkout):
        logger.error("Stay not active")
        raise HTTPException(status_code=401)

    logger.info(
        f"Comparing last names: PMS='{get_guest_last_name(guest)}' vs input='{last_name}'"
    )

    if get_guest_last_name(guest).strip().lower() != last_name.strip().lower():
        logger.error("Last name mismatch")
        raise HTTPException(status_code=401)

    logger.info("Guest validation PASSED")
    return room

# -----------------------------------------------------------------------------
# RAINBOW CONFIG
# -----------------------------------------------------------------------------

def get_rainbow_config():
    return {
        "server": os.getenv("RAINBOW_SERVER"),
        "applicationId": os.getenv("RAINBOW_APP_ID"),
        "secretKey": os.getenv("RAINBOW_APP_SECRET"),
        "guestServiceExt": os.getenv("GUESTSERVICE_EXT"),
        "frontDeskExt": os.getenv("FRONTDESK_EXT"),
        "operatorExt": os.getenv("OPERATOR_EXT"),
        "conciergeExt": os.getenv("CONCIERGE_EXT"),
        "emergencyContact": os.getenv("EMERGENCY_CONTACT"),
    }

@app.get("/api/rainbow/config")
def rainbow_config():
    config = get_rainbow_config()

    if not config["server"] or not config["applicationId"]:
        raise HTTPException(status_code=500, detail="Rainbow config missing")

    return config


# -----------------------------------------------------------------------------
# RAINBOW GUEST CREDENTIALS
# -----------------------------------------------------------------------------

RAINBOW_GUEST_CREDENTIALS: Dict[str, Dict[str, str]] = {
    "1910": {"username": "room1910@hotelaleapac.com", "password": "Rainbow@1910"},
    "1911": {"username": "room1911@hotelaleapac.com", "password": "Rainbow@1911"},
    "1912": {"username": "room1912@hotelaleapac.com", "password": "Rainbow@1912"},
    "1913": {"username": "room1913@hotelaleapac.com", "password": "Rainbow@1913"},
    "1913": {"username": "room1914@hotelaleapac.com", "password": "Rainbow@1914"}
}

def get_rainbow_credentials(room_number: str) -> Dict[str, str]:
    creds = RAINBOW_GUEST_CREDENTIALS.get(room_number)
    if not creds:
        logger.warning(f"No Rainbow credentials configured for room {room_number}")
        raise HTTPException(status_code=401)
    return creds

# -----------------------------------------------------------------------------
# GUEST AUTH API
# -----------------------------------------------------------------------------

@app.post("/api/guest/auth")
async def guest_auth(request: Request):
    body = await request.json()

    room_number = body.get("roomNumber")
    last_name = body.get("lastName")

    logger.info(f"Guest auth attempt room={room_number}")

    await validate_guest(room_number, last_name)
    rainbow = get_rainbow_credentials(room_number)

    logger.info(f"Guest auth OK room={room_number}")

    return {
        "status": "ok",
        "rainbow": rainbow,
    }


# -----------------------------------------------------------------------------
# ROOM SERVICE FORWARDER
# -----------------------------------------------------------------------------

@app.post("/api/flows/new-request")
async def room_service_proxy(payload: dict):
    """
    Forward room service requests to the upstream flow endpoint.
    """
    try:
        async with httpx.AsyncClient(timeout=5, verify=ROOM_SERVICE_VERIFY) as client:
            upstream_resp = await client.post(
                ROOM_SERVICE_URL,
                json={
                    "room_number": payload.get("room_number"),
                    "service_requested": payload.get("service_requested"),
                },
            )
    except httpx.HTTPError as exc:
        logger.error(f"Room service upstream error: {exc}")
        raise HTTPException(status_code=502, detail="Room service unavailable")

    if upstream_resp.status_code >= 400:
        logger.error(
            "Room service upstream returned %s: %s",
            upstream_resp.status_code,
            upstream_resp.text,
        )
        raise HTTPException(
            status_code=upstream_resp.status_code,
            detail="Room service request failed",
        )

    return upstream_resp.json()


# -----------------------------------------------------------------------------
# WAKEUP CALL FORWARDER
# -----------------------------------------------------------------------------

@app.post("/api/wakeup-call")
async def wakeup_call_proxy(payload: dict):
    """
    Schedule a wake-up call via the RHG API.
    """
    room_number = payload.get("room_number")
    alarm_time = payload.get("alarm_time")
    followup_time = payload.get("followup_time")
    frequency = normalize_wakeup_frequency(payload.get("frequency"))

    if not room_number or not alarm_time:
        raise HTTPException(status_code=400, detail="room_number and alarm_time are required")

    pms = await asyncio.to_thread(build_pms_client)
    room = await find_room_by_number(room_number, pms)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    room_id = get_room_id(room)
    if not room_id:
        raise HTTPException(status_code=500, detail="Room id missing in PMS response")

    pms_alarm_time = normalize_wakeup_datetime_for_pms(alarm_time)
    pms_followup_time = normalize_wakeup_datetime_for_pms(followup_time or alarm_time)
    existing_wakeups = await asyncio.to_thread(find_wakeup_calls_for_room_date, pms, room_number, alarm_time)
    existing_wakeup = existing_wakeups[0] if existing_wakeups else None
    existing_wakeup_id = wakeup_id(existing_wakeup) if existing_wakeup else None
    logger.info(
        "%s wakeup call room=%s requested_alarm=%s pms_alarm=%s requested_followup=%s pms_followup=%s existing_id=%s basis=%s timezone=%s",
        "Updating" if existing_wakeup_id else "Creating",
        room_number,
        alarm_time,
        pms_alarm_time,
        followup_time,
        pms_followup_time,
        existing_wakeup_id,
        os.getenv("PMS_WAKEUP_TIME_BASIS", "utc"),
        os.getenv("HOTEL_TIMEZONE", "Asia/Singapore"),
    )

    try:
        if existing_wakeup_id:
            result = await asyncio.to_thread(
                pms.update_wakeup_call,
                wakeup_id=existing_wakeup_id,
                alarm_time=pms_alarm_time,
                followup_time=pms_followup_time,
            )
            deleted_duplicates = await asyncio.to_thread(delete_extra_wakeup_calls, pms, existing_wakeups, existing_wakeup_id)
            if isinstance(result, dict):
                result.setdefault("operation", "updated")
                result.setdefault("wakeup_id", existing_wakeup_id)
                if deleted_duplicates:
                    result["deleted_duplicates"] = deleted_duplicates
            return result
        result = await asyncio.to_thread(
            pms.create_wakeup_call,
            room_id=room_id,
            alarm_time=pms_alarm_time,
            followup_time=pms_followup_time,
            frequency=frequency,
        )
        if isinstance(result, dict):
            result.setdefault("operation", "created")
        return result
    except (ApiError, AuthenticationError, RequestError) as exc:
        logger.error(f"Wakeup upstream error: {exc}")
        raise HTTPException(status_code=502, detail=f"Wakeup service unavailable: {exc}")
