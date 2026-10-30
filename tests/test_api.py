"""Run with:  pytest -q   (from the project folder, venv active)"""

import json
import os
import sys
from pathlib import Path

os.environ["DEMO_TOKEN"] = "test-token"
os.environ.setdefault("FEE_PER_BOOKING", "25")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from starlette.testclient import TestClient

import main
import normalize
import notify

AUTH = {"Authorization": "Bearer test-token"}
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_state():
    main.reset_state()
    yield


def search(**kw):
    body = {"language": "Mandarin", "insurance": "Aetna", "mode": "online", "time_pref": ""}
    body.update(kw)
    return client.post("/search_clinics", json=body, headers=AUTH).json()


def book(**kw):
    body = {"clinic_id": "C001", "slot": "Fri Oct 30 6:00 PM", "caller_name": "Test", "confirmed": "yes"}
    body.update(kw)
    return client.post("/book", json=body, headers=AUTH).json()


# ---------- normalize ----------

@pytest.mark.parametrize("heard,expected", [
    ("Aetna", "Aetna"), ("athena", "Aetna"), ("Etna", "Aetna"), ("aetna ", "Aetna"),
    ("signa", "Cigna"), ("UHC", "UnitedHealthcare"), ("united health care", "UnitedHealthcare"),
    ("medic aid", "Medicaid"), ("Aetna Better Health", "Aetna"), ("Medicare Advantage", "Medicare"), ("self pay", "Self-pay"), ("自费", "Self-pay"),
    ("banana", ""), ("", ""),
])
def test_insurance_normalize(heard, expected):
    assert normalize.insurance(heard) == expected


@pytest.mark.parametrize("heard,expected", [
    ("Chinese", "Mandarin"), ("普通话", "Mandarin"), ("mandarin", "Mandarin"),
    ("espanol", "Spanish"), ("Klingon", ""),
])
def test_language_normalize(heard, expected):
    assert normalize.language(heard) == expected


def test_every_clinic_value_is_canonical():
    """clinics.json must only use names normalize.py knows, or matching silently fails."""
    for c in main.ORIGINAL_CLINICS:
        for ins in c["insurances"]:
            assert normalize.insurance(ins) == ins, (c["clinic_id"], ins)
        for lang in c["languages"]:
            assert normalize.language(lang) == lang, (c["clinic_id"], lang)
        for m in c["mode"]:
            assert normalize.mode(m) == m, (c["clinic_id"], m)


# ---------- search ----------

def test_search_matches_two_mandarin_clinics():
    r = search()
    assert r["ok"] and [m["clinic_id"] for m in r["matches"]] == ["C001", "C002"]


def test_misheard_insurance_still_matches_and_asks_to_confirm():
    r = search(insurance="athena")
    assert r["ok"] and len(r["matches"]) == 2
    assert r["understood"]["insurance"] == "Aetna"
    assert "confirm" in r["message"].lower() and "Aetna" in r["message"]


def test_unrecognised_insurance_asks_to_spell():
    r = search(insurance="banana plan")
    assert not r["ok"] and "spell" in r["message"]


def test_no_match_is_honest():
    r = search(language="Spanish", insurance="Cigna")
    assert r["ok"] and r["matches"] == []


def test_missing_field_refused():
    r = search(mode="%(mode)s")
    assert not r["ok"] and "mode" in r["message"]


def test_wrong_token_401():
    assert client.post("/search_clinics", json={}, headers={"Authorization": "Bearer x"}).status_code == 401


# ---------- booking ----------

def test_booking_rules():
    assert not book(confirmed="no")["ok"]
    assert not book(caller_name="")["ok"]
    assert not book(clinic_id="C999")["ok"]
    assert not book(slot="Mon Nov 2 9:00 AM")["ok"]
    r = book()
    assert r["ok"] and r["booking_id"] == "CM-0001"
    assert not book(slot="fri oct 30, 6:00 pm")["ok"]  # same slot again


def test_booking_records_time_and_fee():
    search(call_id="CA1")
    assert book(call_id="CA1")["ok"]
    s = client.get("/stats").json()
    assert s["bookings"] == 1
    assert s["last_seconds_to_book"] is not None and s["last_seconds_to_book"] >= 0
    assert s["billed_total"] == 25
    assert s["per_clinic"][0]["clinic"] == "Harbor Light Counseling"


def test_refused_booking_is_not_billed():
    book(confirmed="no")
    assert client.get("/stats").json()["billed_total"] == 0


def test_call_status_start_is_used_for_timer(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: t[0])
    client.post("/call_status?token=test-token", json={"status": "in-progress"})
    t[0] = 1030.0
    search(call_id="CA2")
    t[0] = 1095.0
    book(call_id="CA2")
    assert client.get("/stats").json()["last_seconds_to_book"] == 95.0


def test_unbooked_call_does_not_leak_into_next_call(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: t[0])
    search()                                   # call 1 starts, caller hangs up without booking
    t[0] = 1200.0
    client.post("/call_status?token=test-token", json={"status": "completed"})
    t[0] = 1500.0
    search()                                   # call 2 starts
    t[0] = 1560.0
    book()
    assert client.get("/stats").json()["last_seconds_to_book"] == 60.0


def test_call_status_needs_token():
    assert client.post("/call_status", json={"status": "in-progress"}).status_code == 401


# ---------- transfer ----------

def test_transfer_hook_stores_reason_only():
    r = client.post("/transfer_hook", json={"secret": "test-token", "transfer_reason": "consented",
                                            "transcript": "PRIVATE"})
    assert r.json()["ok"]
    details = " ".join(e["detail"] for e in client.get("/events").json())
    assert "consented" in details and "PRIVATE" not in details


def test_transfer_hook_bad_secret():
    assert client.post("/transfer_hook", json={"secret": "nope"}).status_code == 401


# ---------- reset ----------

def test_reset_restores_slots_and_billing():
    book()
    client.post("/reset", headers=AUTH)
    assert client.get("/stats").json()["billed_total"] == 0
    assert book()["ok"]  # slot is free again


# ---------- v2: CareMatch phones clinics for the patient ----------

def report(**kw):
    body = {"clinic_id": "C002", "accepting_new": "yes", "takes_insurance": "yes", "speaks_language": "yes",
            "therapist": "Dr. Chen", "slot": "Thu Nov 5 6:00 PM", "hold": "yes", "declined_ai": "no", "note": ""}
    body.update(kw)
    return client.post("/report_clinic", json=body, headers=AUTH).json()


def board_rows():
    return {c["clinic_id"]: c for c in client.get("/board").json()["clinics"]}


def test_search_opens_request_without_times():
    r = search()
    assert all("open_slots" not in m for m in r["matches"])
    assert "Do not offer times" in r["message"]
    rows = board_rows()
    assert set(rows) == {"C001", "C002"} and all(c["status"] == "to_call" for c in rows.values())


def test_report_verified():
    search()
    r = report()
    assert r["ok"] and "end the call" in r["message"]
    row = board_rows()["C002"]
    assert row["status"] == "verified" and row["therapist"] == "Dr. Chen" and row["verified_at"]
    assert any("verified by phone" in e["detail"] for e in client.get("/events").json())


def test_report_ruled_out_with_reason():
    search()
    r = report(clinic_id="C001", takes_insurance="NO", note="stopped taking Aetna last year", hold="no", slot="")
    assert r["ok"]
    row = board_rows()["C001"]
    assert row["status"] == "ruled_out"
    assert "doesn't take Aetna" in row["reason"] and "stopped taking Aetna" in row["reason"]


def test_report_unknown_answers_rule_out():
    search()
    report(speaks_language="not sure")
    assert board_rows()["C002"]["status"] == "ruled_out"


def test_declined_ai_goes_on_do_not_call_list():
    search()
    report(clinic_id="C001", declined_ai="yes")
    assert board_rows()["C001"]["status"] == "declined_ai"
    again = report(clinic_id="C001")
    assert not again["ok"] and "not to receive automated calls" in again["message"]
    r = search()  # a new request no longer asks to call it
    assert [m["clinic_id"] for m in r["matches"]] == ["C002"]


def test_report_unknown_clinic():
    assert not report(clinic_id="C999")["ok"]


def test_results_reads_back_verified_and_ruled_out():
    search()
    report(clinic_id="C001", takes_insurance="no", hold="no", slot="")
    report()
    r = client.post("/results", json={}, headers=AUTH).json()
    assert r["ok"] and r["verified"][0]["clinic_id"] == "C002"
    assert r["ruled_out"][0]["name"] == "Harbor Light Counseling"
    assert "confirmed by phone" in r["message"] and "Thu Nov 5 6:00 PM" in r["message"]


def test_results_with_nothing_verified_is_honest():
    search()
    r = client.post("/results", json={}, headers=AUTH).json()
    assert r["verified"] == [] and "Do not offer any times" in r["message"]


def test_book_held_slot_after_verification():
    search()
    report()
    r = book(clinic_id="C002", slot="thu nov 5 6:00 pm")
    assert r["ok"] and "verified by phone" in r["message"] and "intake forms" in r["message"]
    assert board_rows()["C002"]["status"] == "booked"
    assert client.get("/stats").json()["billed_total"] == 25
    assert not book(clinic_id="C002", slot="Thu Nov 5 6:00 PM")["ok"]  # no double booking


def test_book_unheld_slot_still_refused():
    search()
    report()
    assert not book(clinic_id="C002", slot="Fri Nov 6 9:00 AM")["ok"]


def test_recording_url_kept_but_not_transcript():
    search()
    report()
    client.post("/call_webhook?secret=test-token", json={
        "status": "completed", "transcript": "PRIVATE WORDS",
        "telephony_data": {"recording_url": "https://rec.example/1.mp3"}})
    assert board_rows()["C002"]["recording"] is True
    assert main.STATE["recordings"] == {"C002": "https://rec.example/1.mp3"}
    assert "PRIVATE" not in json.dumps(main.STATE, default=str)
    # the next call ending (the patient callback) must not overwrite it
    client.post("/call_webhook?secret=test-token", json={
        "status": "completed", "telephony_data": {"recording_url": "https://rec.example/2.mp3"}})
    assert main.STATE["recordings"] == {"C002": "https://rec.example/1.mp3"}


def test_transfer_event_text():
    client.post("/transfer_hook", json={"secret": "test-token", "transfer_reason": "x"})
    assert client.get("/events").json()[0]["detail"].startswith("Human callback requested")


def test_reset_clears_v2_state():
    search()
    report(clinic_id="C001", declined_ai="yes")
    client.post("/reset", headers=AUTH)
    assert client.get("/board").json()["request"] is None
    assert main.STATE["dnc"] == set()


def test_email_skipped_without_settings(monkeypatch):
    monkeypatch.delenv("CONFIRM_EMAIL", raising=False)
    ok, detail = notify.send_confirmation({"booking_id": "CM-1", "clinic_name": "X", "slot": "Thu Nov 5 6:00 PM"})
    assert not ok and "skipped" in detail


def test_email_composed_with_calendar_invite():
    subject, text, ics = notify.compose({"booking_id": "CM-0001", "clinic_name": "Bamboo Grove Wellness",
                                         "slot": "Thu Nov 5 6:00 PM", "therapist": "Dr. Chen",
                                         "verified_at": "11:02 PM", "recording_url": "https://rec.example/1.mp3"})
    assert "CM-0001" in subject and "front desk by phone at 11:02 PM" in text and "rec.example" in text
    assert "BEGIN:VCALENDAR" in ics and "DTSTART:20261105T230000Z" in ics


@pytest.mark.parametrize("text,ok", [("Thu Nov 5 6:00 PM", True), ("Thursday 6pm", True),
                                     ("Nov 5 at 18:00", True), ("sometime next week", False)])
def test_parse_slot(text, ok):
    assert (notify.parse_slot(text) is not None) == ok


def test_ruled_out_reason_lists_only_real_problems():
    search()
    report(clinic_id="C001", takes_insurance="no", hold="no", slot="")
    assert board_rows()["C001"]["reason"] == "doesn't take Aetna"
    report(clinic_id="C002", hold="no", slot="")
    assert board_rows()["C002"]["reason"] == "couldn't hold a slot"
