import os
import datetime as dt
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
APP_BASE_URL = os.environ["APP_BASE_URL"].rstrip("/")
JOB_SECRET = os.environ["JOB_SECRET"]

app = FastAPI()

# Allow your frontend domain to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later to your Cloudflare Pages URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _supabase_headers():
    return {
        "apikey": SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }

def _pt_now():
    # PT = America/Los_Angeles; without extra deps, approximate by using system UTC and offset rules is messy.
    # For reliability: we don't convert timezones here; instead we store open times in DB (UTC) and use UTC checks.
    return dt.datetime.utcnow()

async def get_next_match(client: httpx.AsyncClient):
    # Get next upcoming match (by date)
    url = f"{SUPABASE_URL}/rest/v1/matches"
    params = {
        "select": "*",
        "order": "match_date.asc",
        "limit": "1",
        "match_date": f"gte.{dt.date.today().isoformat()}",
    }
    r = await client.get(url, headers=_supabase_headers(), params=params)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None

def build_message(match: dict, audience: str) -> str:
    date_str = match["match_date"]
    time_str = match["start_time"][:5]
    location = match["location"]
    spots = match["max_players"]
    rsvp_link = f"{APP_BASE_URL}/#rsvp?match_id={match['id']}&aud={audience}"

    if audience == "earlybird":
        return (
            "🏏 Weekend Cricket RSVP (Early Bird)\n"
            f"📅 {date_str} (Sat) • ⏰ {time_str}\n"
            f"📍 {location}\n"
            f"✅ RSVP here: {rsvp_link}\n"
            "Early-bird window is open now."
        )
    return (
        "🏏 Weekend Cricket RSVP Open\n"
        f"📅 {date_str} (Sat) • ⏰ {time_str}\n"
        f"📍 {location}\n"
        f"✅ RSVP here: {rsvp_link}\n"
        f"Spots: {spots} • First come first serve."
    )

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/admin/jobs/generate_whatsapp_draft")
async def generate_whatsapp_draft(authorization: str = Header(default="")):
    # Protect this endpoint (called by cron)
    if authorization != f"Bearer {JOB_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with httpx.AsyncClient(timeout=20) as client:
        match = await get_next_match(client)
        if not match:
            return {"ok": True, "message": "No upcoming match found"}

        now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

        # We rely on DB-stored UTC timestamps for open times
        early_open = dt.datetime.fromisoformat(match["earlybird_open_at"].replace("Z", "+00:00"))
        gen_open = dt.datetime.fromisoformat(match["general_open_at"].replace("Z", "+00:00"))

        tasks = []
        # Within a 5-minute window after opening time, create draft if missing
        if early_open <= now_utc <= early_open + dt.timedelta(minutes=5):
            tasks.append("earlybird")
        if gen_open <= now_utc <= gen_open + dt.timedelta(minutes=5):
            tasks.append("general")

        created = []
        for audience in tasks:
            msg = build_message(match, audience)
            url = f"{SUPABASE_URL}/rest/v1/message_drafts"
            payload = {
                "match_id": match["id"],
                "audience": audience,
                "message_text": msg,
                "status": "ready",
            }
            # upsert-like behavior using Prefer header + unique constraint
            r = await client.post(
                url,
                headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
                json=payload,
            )
            r.raise_for_status()
            created.append(audience)

        return {"ok": True, "match_id": match["id"], "created": created}
