"""CareMatch demo backend, v2: CareMatch phones clinics for the patient.

Flow (three Bolna agents, one backend):
  1. Intake agent: patient calls in -> /search_clinics opens a request; matched
     clinics go on the "Clinic check" board as "to call". No times offered yet.
  2. Caller agent: phones each clinic, asks four questions, reports the answers
     -> /report_clinic marks the clinic verified, ruled out, or do-not-call.
  3. Callback agent: calls the patient back -> /results reads what was confirmed,
     /book books the slot the clinic is holding, a confirmation email goes out.

Endpoints
  POST /search_clinics   intake tool                     (Bearer token)
  POST /report_clinic    caller tool                     (Bearer token)
  POST /results          callback tool                   (Bearer token)
  POST /book             callback tool                   (Bearer token)
  POST /transfer_hook    Bolna pre-transfer webhook      (secret in body)
  POST /call_status      Bolna call webhook              (?token= or ?secret=)
  POST /call_webhook     same handler, name used in the v2 proposal
  GET  /events /stats /board   dashboard feeds
  GET  /dashboard        dashboard page
  POST /reset            restore everything               (Bearer token)

Bolna sends every tool value as a string. Business outcomes, including
rejections, return HTTP 200 with {"ok": ..., "message": "..."} so the agent
can follow the message. Only bad credentials return 401.
"""

import copy
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

import normalize
import notify

BASE = Path(__file__).parent
DEMO_TOKEN = os.environ.get("DEMO_TOKEN", "")
FEE_PER_BOOKING = float(os.environ.get("FEE_PER_BOOKING", "25"))  # USD per confirmed booking
CALL_START_MAX_AGE = 30 * 60  # seconds; an unclaimed call start older than this is ignored

with open(BASE / "clinics.json", encoding="utf-8") as f:
    ORIGINAL_CLINICS = json.load(f)

LOCK = threading.Lock()
STATE = {}


def reset_state():
    STATE["clinics"] = {c["clinic_id"].upper(): copy.deepcopy(c) for c in ORIGINAL_CLINICS}
    STATE["bookings"] = {}
    STATE["next_id"] = 1
    STATE["events"] = deque(maxlen=200)
    STATE["call_starts"] = {}       # call_id -> start time (epoch seconds)
    STATE["pending_starts"] = []    # starts reported by /call_status, not yet claimed by a tool call
    STATE["request"] = None         # the patient's current request, see new_request()
    STATE["dnc"] = set()            # clinic_ids that asked not to receive automated calls
    STATE["recordings"] = {}        # clinic_id -> recording_url of the verification call
    STATE["last_reported"] = ""     # clinic_id of the most recent /report_clinic, for recordings


reset_state()


# ---------- helpers ----------

def log_event(type_, detail):
    """One short line per event. Never pass conversation text in here."""
    STATE["events"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": type_,
        "detail": detail[:220],
    })


def clean(value):
    """Bolna fills params with %(name)s; an unfilled one may arrive as the raw
    placeholder, 'None', 'null' or empty. Treat all of those as empty."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.startswith("%(") or s.lower() in {"none", "null", "undefined", "n/a"}:
        return ""
    return s


def yes_no(value):
    s = clean(value).lower()
    if s in {"yes", "y", "true", "1"}:
        return "yes"
    if s in {"no", "n", "false", "0"}:
        return "no"
    return "unknown"


def norm_slot(s):
    return re.sub(r"\s+", " ", s.strip().lower()).replace(",", "")


def fmt_duration(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%-I:%M %p")


def call_start(call_id):
    """When did this call start? Must be called with LOCK held.

    1. Already known for this call_id -> use it.
    2. Otherwise claim the newest unclaimed start from /call_status.
    3. Otherwise this tool call is the first sign of the call: start now.
    Calls without a call_id share the key "current" (fine for a one-caller demo).
    """
    key = call_id or "current"
    now = time.time()
    known = STATE["call_starts"].get(key)
    if known is not None and now - known <= CALL_START_MAX_AGE:
        return known
    pending = [t for t in STATE["pending_starts"] if now - t <= CALL_START_MAX_AGE]
    start = pending.pop() if pending else now
    STATE["pending_starts"] = pending
    STATE["call_starts"][key] = start
    return start


def end_call(call_id):
    STATE["call_starts"].pop(call_id or "current", None)


def new_request(criteria, clinic_ids, started):
    """The patient's request plus one verification row per matched clinic."""
    rows = {}
    for cid in clinic_ids:
        rows[cid] = {
            "status": "declined_ai" if cid in STATE["dnc"] else "to_call",
            "accepting_new": "", "takes_insurance": "", "speaks_language": "", "hold": "",
            "therapist": "", "slot": "", "note": "", "reason": "", "verified_at": None,
        }
    return {**criteria, "created_at": started, "order": list(clinic_ids), "rows": rows}


def ensure_row(clinic_id):
    """A clinic reported outside the current request still gets a board row."""
    req = STATE["request"]
    if req is None:
        req = STATE["request"] = new_request(
            {"language": "", "insurance": "", "mode": "", "time_pref": ""}, [], time.time())
    if clinic_id not in req["rows"]:
        req["rows"].update(new_request({}, [clinic_id], 0)["rows"])
        req["order"].append(clinic_id)
    return req["rows"][clinic_id]


def ok_token(request):
    if not DEMO_TOKEN:
        return False
    return request.headers.get("authorization", "") == f"Bearer {DEMO_TOKEN}"


async def read_body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def unauthorized():
    return JSONResponse({"ok": False, "message": "Unauthorized."}, status_code=401)


# ---------- 1. intake agent ----------

async def search_clinics(request: Request):
    if not ok_token(request):
        log_event("auth", "Rejected request with wrong token: /search_clinics")
        return unauthorized()
    body = await read_body(request)
    heard = {k: clean(body.get(k)) for k in ("language", "insurance", "mode")}
    time_pref = clean(body.get("time_pref"))
    call_id = clean(body.get("call_id"))

    missing = [k for k, v in heard.items() if not v]
    if missing:
        log_event("search", f"Search refused: missing {', '.join(missing)}")
        return JSONResponse({
            "ok": False, "matches": [],
            "message": f"Ask the caller for their {' and '.join(missing)} before searching.",
        })

    understood = {
        "language": normalize.language(heard["language"]),
        "insurance": normalize.insurance(heard["insurance"]),
        "mode": normalize.mode(heard["mode"]),
    }
    unknown = [k for k, v in understood.items() if not v]
    if unknown:
        what = ", ".join(f"{k} '{heard[k]}'" for k in unknown)
        log_event("search", f"Not recognised: {what}")
        return JSONResponse({
            "ok": False, "matches": [], "understood": understood,
            "message": (f"I did not recognise the {what}. Ask the caller to repeat it, "
                        "or to spell it letter by letter, then search again."),
        })

    # Only insurance and language are worth reading back; a mode alias ("virtual") is unambiguous.
    corrected = {k: (heard[k], understood[k]) for k in ("insurance", "language")
                 if heard[k].strip().lower() != understood[k].lower()}

    with LOCK:
        started = call_start(call_id)
        matches = []
        for c in STATE["clinics"].values():
            if understood["language"] not in c["languages"]:
                continue
            if understood["insurance"] not in c["insurances"]:
                continue
            if understood["mode"] not in c["mode"]:
                continue
            matches.append({
                "clinic_id": c["clinic_id"],
                "name": c["name"],
                "why": (f"Listed as speaking {understood['language']}, accepting {understood['insurance']}, "
                        f"offering {understood['mode'].replace('_', ' ')} sessions."),
            })
            if len(matches) == 3:
                break
        criteria = {**understood, "time_pref": time_pref}
        STATE["request"] = new_request(criteria, [m["clinic_id"] for m in matches], started)
        to_call = [m for m in matches if m["clinic_id"] not in STATE["dnc"]]

    summary = f"{understood['language']} / {understood['insurance']} / {understood['mode']}"
    if time_pref:
        summary += f" / {time_pref}"
    fixes = "; ".join(f"heard '{h}' -> {u}" for h, u in corrected.values())
    note = f" ({fixes})" if fixes else ""
    confirm = ""
    if corrected:
        confirm = (" First confirm with the caller: "
                   + "; ".join(f"you said {u}, is that right?" for _, u in corrected.values()) + " ")

    log_event("search", f"New request: {summary}{note} -> {len(to_call)} clinic(s) to call")
    if not to_call:
        return JSONResponse({
            "ok": True, "matches": [], "understood": understood,
            "message": (confirm + "No clinic in the directory matches all of these. "
                        "Say so honestly and offer to relax one filter.").strip(),
        })
    return JSONResponse({
        "ok": True, "matches": to_call, "understood": understood,
        "message": (confirm + f"Found {len(to_call)} clinic(s) listed in the directory. Name them, "
                    "explain that listings are often out of date so CareMatch will phone each one "
                    "to confirm insurance, language and openings, then call back. "
                    "Do not offer times or book on this call.").strip(),
    })


# ---------- 2. caller agent ----------

async def report_clinic(request: Request):
    if not ok_token(request):
        log_event("auth", "Rejected request with wrong token: /report_clinic")
        return unauthorized()
    body = await read_body(request)
    clinic_id = clean(body.get("clinic_id")).upper()
    answers = {k: yes_no(body.get(k))
               for k in ("accepting_new", "takes_insurance", "speaks_language", "hold", "declined_ai")}
    therapist = clean(body.get("therapist"))
    slot = clean(body.get("slot"))
    note = clean(body.get("note"))[:120]

    with LOCK:
        clinic = STATE["clinics"].get(clinic_id)
        if not clinic:
            return JSONResponse({"ok": False, "message": f"Unknown clinic_id {clinic_id or '(empty)'}. "
                                 "Use the clinic_id you were given for this call."})
        name = clinic["name"]
        if clinic_id in STATE["dnc"]:
            log_event("report", f"Blocked: {name} is on the do-not-call list")
            return JSONResponse({"ok": False, "message": "This clinic asked not to receive automated calls. "
                                 "Apologize and end the call."})

        row = ensure_row(clinic_id)
        req = STATE["request"]
        ins = req["insurance"] or "insurance"
        lang = req["language"] or "language"
        STATE["last_reported"] = clinic_id
        row.update({k: answers[k] for k in ("accepting_new", "takes_insurance", "speaks_language", "hold")})
        row.update({"therapist": therapist, "slot": slot, "note": note})

        if answers["declined_ai"] == "yes":
            STATE["dnc"].add(clinic_id)
            row["status"] = "declined_ai"
            row["reason"] = "asked not to get automated calls"
            event = ("report", f"{name} asked not to get automated calls, added to do-not-call list")
        elif (answers["accepting_new"] == answers["takes_insurance"] == answers["speaks_language"]
              == answers["hold"] == "yes" and slot):
            row["status"] = "verified"
            row["verified_at"] = time.time()
            row["reason"] = ""
            who = f" ({therapist})" if therapist else ""
            event = ("verified", f"{name} verified by phone: accepting ✓ {ins} ✓ {lang} ✓{who}, {slot} held")
        else:
            labels = {
                "accepting_new": "not accepting new patients",
                "takes_insurance": f"doesn't take {ins}",
                "speaks_language": f"no {lang}-speaking therapist",
            }
            parts = [labels[k] for k in labels if answers[k] == "no"]
            unsure = [k.replace("_", " ") for k in labels if answers[k] == "unknown"]
            if not parts and not unsure:  # the clinic fits; only the hold failed
                parts.append("couldn't hold a slot" if answers["hold"] != "yes" else "no slot time given")
            if unsure and not parts:
                parts.append("couldn't confirm " + ", ".join(unsure))
            if note:
                parts.append(note)
            row["status"] = "ruled_out"
            row["reason"] = "; ".join(parts) or "not confirmed"
            event = ("ruled_out", f"{name} ruled out by phone: {row['reason']}")

    log_event(*event)
    return JSONResponse({"ok": True, "message": "Recorded. Thank the front desk and end the call."})


# ---------- 3. callback agent ----------

async def results(request: Request):
    if not ok_token(request):
        log_event("auth", "Rejected request with wrong token: /results")
        return unauthorized()
    with LOCK:
        req = STATE["request"]
        rows = [(cid, req["rows"][cid]) for cid in req["order"]] if req else []
        verified = [{"clinic_id": cid, "name": STATE["clinics"][cid]["name"], "therapist": r["therapist"],
                     "slot": r["slot"], "verified_at": clock(r["verified_at"])}
                    for cid, r in rows if r["status"] == "verified"]
        ruled_out = [{"name": STATE["clinics"][cid]["name"], "reason": r["reason"]}
                     for cid, r in rows if r["status"] in {"ruled_out", "declined_ai"}]
        pending = sum(1 for _, r in rows if r["status"] == "to_call")
        ins = (req or {}).get("insurance") or "their insurance"
        lang = (req or {}).get("language") or "their language"

    if verified:
        lines = []
        for v in verified:
            who = f"{v['therapist']} speaks {lang}" if v["therapist"] else f"a therapist speaks {lang}"
            lines.append(f"{v['name']} confirmed by phone at {v['verified_at']}: they are accepting new "
                         f"patients, take {ins}, {who}, and are holding {v['slot']} for 24 hours.")
        msg = " ".join(lines)
        if ruled_out:
            msg += " Briefly mention the clinics that did not work out: " + "; ".join(
                f"{r['name']} ({r['reason']})" for r in ruled_out) + "."
        msg += (" Offer the held slot and ask if the caller wants to book it. Before booking, ask for "
                "their name, repeat the clinic and time back, and wait for a clear yes.")
    else:
        msg = "None of the clinics could be confirmed by phone yet. Tell the caller honestly"
        if ruled_out:
            msg += ", say briefly why: " + "; ".join(f"{r['name']} ({r['reason']})" for r in ruled_out)
        msg += ". Offer to try more clinics. Do not offer any times."
    if pending:
        msg += f" {pending} clinic(s) have not been reached yet."

    log_event("results", "Results read back to patient")
    return JSONResponse({"ok": True, "verified": verified, "ruled_out": ruled_out, "message": msg})


async def book(request: Request):
    if not ok_token(request):
        log_event("auth", "Rejected request with wrong token: /book")
        return unauthorized()
    body = await read_body(request)
    clinic_id = clean(body.get("clinic_id")).upper()
    slot = clean(body.get("slot"))
    caller_name = clean(body.get("caller_name"))
    confirmed = clean(body.get("confirmed")).lower()
    call_id = clean(body.get("call_id"))

    def refuse(reason, message):
        log_event("booking_refused", f"Booking refused: {reason}")
        return JSONResponse({"ok": False, "message": message})

    if confirmed not in {"yes", "true"}:
        return refuse("not confirmed", "Booking needs the caller's clear confirmation first.")
    if not caller_name:
        return refuse("no caller name", "Ask for the caller's name before booking.")

    with LOCK:
        clinic = STATE["clinics"].get(clinic_id)
        if not clinic:
            return refuse(f"unknown clinic {clinic_id or '(empty)'}",
                          "That clinic is not in our partner list. Only book clinics returned by the search.")
        wanted = norm_slot(slot)
        if any(norm_slot(b["slot"]) == wanted and b["clinic_id"] == clinic_id
               for b in STATE["bookings"].values()):
            return refuse(f"slot already booked at {clinic['name']}",
                          "That time slot was just booked. Offer the caller one of the other open slots.")

        req = STATE["request"]
        row = req["rows"].get(clinic_id) if req else None
        held = row and row["status"] == "verified" and norm_slot(row["slot"]) == wanted
        exact = row["slot"] if held else next((s for s in clinic["open_slots"] if norm_slot(s) == wanted), None)
        if not exact:
            return refuse(f"slot not offered at {clinic['name']}",
                          "That time slot is not available at this clinic. Offer only the slot the clinic is holding.")

        if req and req["created_at"]:
            elapsed = time.time() - req["created_at"]   # first patient call -> booked, across all calls
        else:
            elapsed = time.time() - call_start(call_id)
        elapsed = max(0.0, elapsed)
        end_call(call_id)
        if exact in clinic["open_slots"]:
            clinic["open_slots"].remove(exact)
        booking_id = f"CM-{STATE['next_id']:04d}"
        STATE["next_id"] += 1
        booking = {
            "booking_id": booking_id, "clinic_id": clinic_id,
            "clinic_name": clinic["name"], "slot": exact, "caller_name": caller_name,
            "therapist": row["therapist"] if held else "",
            "verified_at": clock(row["verified_at"]) if held else "",
            "recording_url": STATE["recordings"].get(clinic_id, ""),
            "seconds_to_book": round(elapsed, 1), "fee": FEE_PER_BOOKING,
        }
        STATE["bookings"][booking_id] = booking
        if row:
            row["status"] = "booked"

    via = f", verified by phone at {booking['verified_at']}" if held else ""
    log_event("booking", (f"Booking {booking_id} written: {clinic['name']}, {exact}{via} · "
                          f"{fmt_duration(elapsed)} from first call · ${FEE_PER_BOOKING:g} billed to clinic"))
    threading.Thread(target=send_confirmation, args=(dict(booking),), daemon=True).start()

    if held:
        msg = (f"Booked {clinic['name']} for {exact}, verified by phone at {booking['verified_at']}. "
               f"Give the caller reference {booking_id} and say the clinic will email intake forms.")
    else:
        msg = (f"Booked {clinic['name']} for {exact}. Tell the caller the booking is confirmed "
               f"and their reference is {booking_id}.")
    return JSONResponse({"ok": True, "booking_id": booking_id, "message": msg})


def send_confirmation(booking):
    """Runs in a background thread so the agent is never kept waiting on email."""
    ok, detail = notify.send_confirmation(booking)
    with LOCK:
        log_event("email" if ok else "email_failed", detail)


# ---------- Bolna webhooks ----------

async def transfer_hook(request: Request):
    body = await read_body(request)
    secret = clean(body.get("secret"))
    if not DEMO_TOKEN or secret != DEMO_TOKEN:
        log_event("auth", "Rejected transfer webhook with wrong secret")
        return unauthorized()
    reason = clean(body.get("transfer_reason")) or "not given"
    call_id = clean(body.get("call_id"))
    with LOCK:
        elapsed = time.time() - call_start(call_id)
        end_call(call_id)
    # Only the reason is recorded. The call record in the body is ignored on purpose.
    log_event("transfer", f"Human callback requested {fmt_duration(elapsed)} after call start. "
                          f"Reason: {reason[:100]}")
    return JSONResponse({"ok": True})


async def call_status(request: Request):
    """Bolna agent webhook, sent on every call status change.
    Bolna cannot add headers here, so the token goes in the URL (?token= or ?secret=).
    Only the status and, for completed clinic calls, telephony_data.recording_url are
    read. The transcript and the rest of the call record are never stored."""
    given = request.query_params.get("token") or request.query_params.get("secret")
    if not DEMO_TOKEN or given != DEMO_TOKEN:
        return unauthorized()
    body = await read_body(request)
    status = clean(body.get("status")).lower().replace("_", "-")
    if status == "in-progress":
        with LOCK:
            STATE["pending_starts"].append(time.time())
        log_event("call", "Call connected")
    elif status in {"completed", "call-disconnected"}:
        telephony = body.get("telephony_data") if isinstance(body.get("telephony_data"), dict) else {}
        url = clean(telephony.get("recording_url"))
        with LOCK:
            # We cannot map webhook ids to the tools' call_id, so an ended call clears "current".
            end_call("")
            clinic_id = STATE["last_reported"]
            STATE["last_reported"] = ""
            if url and clinic_id:
                STATE["recordings"][clinic_id] = url
                name = STATE["clinics"][clinic_id]["name"]
        log_event("call", f"Call ended · recording saved for {name}" if url and clinic_id else "Call ended")
    return JSONResponse({"ok": True})


# ---------- dashboard ----------

async def events(request: Request):
    items = list(STATE["events"])[-50:]
    items.reverse()
    return JSONResponse(items)


async def stats(request: Request):
    with LOCK:
        bookings = list(STATE["bookings"].values())
    times = [b["seconds_to_book"] for b in bookings]
    per_clinic = {}
    for b in bookings:
        row = per_clinic.setdefault(b["clinic_name"], {"clinic": b["clinic_name"], "bookings": 0, "billed": 0.0})
        row["bookings"] += 1
        row["billed"] += b["fee"]
    return JSONResponse({
        "bookings": len(bookings),
        "last_seconds_to_book": times[-1] if times else None,
        "avg_seconds_to_book": round(sum(times) / len(times), 1) if times else None,
        "fee_per_booking": FEE_PER_BOOKING,
        "billed_total": sum(b["fee"] for b in bookings),
        "per_clinic": sorted(per_clinic.values(), key=lambda r: -r["billed"]),
    })


async def board(request: Request):
    """Directory listing vs what the front desk said on the phone, per clinic."""
    with LOCK:
        req = STATE["request"]
        if not req:
            return JSONResponse({"request": None, "clinics": []})
        rows = []
        for cid in req["order"]:
            r, c = req["rows"][cid], STATE["clinics"][cid]
            rows.append({
                "clinic_id": cid, "name": c["name"],
                "directory": {"languages": c["languages"], "insurances": c["insurances"],
                              "mode": [m.replace("_", " ") for m in c["mode"]]},
                "phone": {k: r[k] for k in ("accepting_new", "takes_insurance", "speaks_language", "hold")},
                "therapist": r["therapist"], "slot": r["slot"], "status": r["status"], "reason": r["reason"],
                "verified_at": clock(r["verified_at"]) if r["verified_at"] else "",
                "recording": cid in STATE["recordings"],
            })
        crit = {k: req[k] for k in ("language", "insurance", "mode", "time_pref")}
    return JSONResponse({"request": crit, "clinics": rows})


async def dashboard(request: Request):
    return FileResponse(BASE / "dashboard.html")


async def reset(request: Request):
    if not ok_token(request):
        return unauthorized()
    with LOCK:
        reset_state()
    log_event("system", "Demo data reset")
    return JSONResponse({"ok": True, "message": "Reset done."})


async def root(request: Request):
    return RedirectResponse("/dashboard")


app = Starlette(routes=[
    Route("/", root),
    Route("/search_clinics", search_clinics, methods=["POST"]),
    Route("/report_clinic", report_clinic, methods=["POST"]),
    Route("/results", results, methods=["POST"]),
    Route("/book", book, methods=["POST"]),
    Route("/transfer_hook", transfer_hook, methods=["POST"]),
    Route("/call_status", call_status, methods=["POST"]),
    Route("/call_webhook", call_status, methods=["POST"]),
    Route("/events", events),
    Route("/stats", stats),
    Route("/board", board),
    Route("/dashboard", dashboard),
    Route("/reset", reset, methods=["POST"]),
])

if not DEMO_TOKEN:
    print("WARNING: DEMO_TOKEN is not set. Every protected request will be rejected.")
