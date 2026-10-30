"""Booking confirmation email with an .ics calendar invite. Standard library only.

Configure with environment variables (never in a file):
  CONFIRM_EMAIL        where the confirmation goes (the demo patient's inbox). Unset = skip.
  RESEND_API_KEY       send through Resend (https://resend.com). Preferred.
  CONFIRM_FROM         Resend sender; default "CareMatch <onboarding@resend.dev>", which only
                       delivers to the email address that owns the Resend account.
  GMAIL_USER / GMAIL_APP_PASSWORD   fallback: Gmail SMTP with an app password.
  CLINIC_TZ            timezone of the slot times, default America/New_York.

send_confirmation() returns (ok, one-line event text). It never raises.
"""

import base64
import json
import os
import re
import smtplib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

SESSION_MINUTES = 50
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_slot(text, now=None):
    """'Thu Nov 5 6:00 PM', 'Thursday 6pm', 'Nov 5 at 18:00' -> aware datetime, or None.
    Without a month and day, the next matching weekday is used."""
    tz = ZoneInfo(os.environ.get("CLINIC_TZ", "America/New_York"))
    now = now or datetime.now(tz)
    s = text.lower()
    month_day = r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b"
    t = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d])", re.sub(month_day, " ", s))
    if not t:
        return None
    hour, minute, ampm = int(t.group(1)), int(t.group(2) or 0), (t.group(3) or "").replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None

    md = re.search(month_day, s)
    if md:
        month, day = MONTHS.index(md.group(1)) + 1, int(md.group(2))
        try:
            when = datetime(now.year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            return None
        if when < now - timedelta(days=60):
            when = when.replace(year=now.year + 1)
        return when

    wd = re.search(r"\b(" + "|".join(WEEKDAYS) + r")[a-z]*", s)
    if wd:
        ahead = (WEEKDAYS.index(wd.group(1)) - now.weekday()) % 7
        day = now + timedelta(days=ahead)
        when = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=7)
        return when
    return None


def build_ics(booking, start):
    def utc(d):
        return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    end = start + timedelta(minutes=SESSION_MINUTES)
    who = f" with {booking['therapist']}" if booking.get("therapist") else ""
    desc = f"CareMatch booking {booking['booking_id']}."
    if booking.get("verified_at"):
        desc += f" Confirmed by the clinic's front desk by phone at {booking['verified_at']}."
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CareMatch//Demo//EN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@carematch.demo",
        f"DTSTAMP:{utc(datetime.now(timezone.utc))}",
        f"DTSTART:{utc(start)}", f"DTEND:{utc(end)}",
        f"SUMMARY:Therapy session at {booking['clinic_name']}{who}",
        f"DESCRIPTION:{desc}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def compose(booking):
    """Returns (subject, text body, ics text or None)."""
    who = booking.get("therapist") or "your therapist"
    lines = [
        f"Your appointment is booked. Reference: {booking['booking_id']}",
        "",
        f"Clinic:    {booking['clinic_name']}",
        f"Time:      {booking['slot']}",
        f"Therapist: {who}",
    ]
    if booking.get("verified_at"):
        lines += ["", f"Confirmed by the clinic's front desk by phone at {booking['verified_at']}."]
    if booking.get("recording_url"):
        lines += [f"Listen to that call: {booking['recording_url']}"]
    lines += ["", "The clinic will email you intake forms.",
              "If you are in crisis, call or text 988 (US), or 911 in an emergency.",
              "", "CareMatch (demo; all clinics are fictional)"]
    start = parse_slot(booking["slot"])
    ics = build_ics(booking, start) if start else None
    if not ics:
        lines.insert(4, "(Calendar invite not attached: the slot time could not be read.)")
    subject = f"Booked: {booking['clinic_name']}, {booking['slot']} ({booking['booking_id']})"
    return subject, "\n".join(lines), ics


def _send_resend(to, subject, text, ics):
    payload = {
        "from": os.environ.get("CONFIRM_FROM", "CareMatch <onboarding@resend.dev>"),
        "to": [to], "subject": subject, "text": text,
    }
    if ics:
        payload["attachments"] = [{"filename": "appointment.ics",
                                   "content": base64.b64encode(ics.encode()).decode()}]
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                 "Content-Type": "application/json", "User-Agent": "carematch-demo"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def _send_gmail(to, subject, text, ics):
    user = os.environ["GMAIL_USER"]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"CareMatch <{user}>", to, subject
    msg.set_content(text)
    if ics:
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="appointment.ics")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
        s.login(user, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def send_confirmation(booking):
    to = os.environ.get("CONFIRM_EMAIL", "").strip()
    if not to:
        return False, "Confirmation email skipped: CONFIRM_EMAIL not set"
    subject, text, ics = compose(booking)
    try:
        if os.environ.get("RESEND_API_KEY"):
            _send_resend(to, subject, text, ics)
        elif os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
            _send_gmail(to, subject, text, ics)
        else:
            return False, "Confirmation email skipped: no RESEND_API_KEY or Gmail settings"
    except urllib.error.HTTPError as e:
        return False, f"Confirmation email failed: Resend said {e.code}"
    except Exception as e:  # network, auth, SMTP: report, never crash the booking
        return False, f"Confirmation email failed: {type(e).__name__}"
    extra = " with calendar invite" if ics else ""
    rec = " and call recording link" if booking.get("recording_url") else ""
    return True, f"Confirmation email sent{extra}{rec} for {booking['booking_id']}"
