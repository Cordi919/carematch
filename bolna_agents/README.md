# Bolna agents

CareMatch runs as three agents in Bolna's Agent Studio, plus one backup. All three use Bolna's cascaded voice engine (speech-to-text → LLM → text-to-speech) and call this repo's backend as tools.

This page records each agent's configuration as set up in Agent Studio on Oct 30, 2026. Text in quotes is copied from the agent; `[…]` marks where the Agent Studio editor cut a long field off and the rest was not captured. Raw JSON exports from Agent Studio (Export button) will be added here; they must have API keys, phone numbers and the demo token removed before committing.

| Agent | Talks to | Backend tools |
|---|---|---|
| **CareMatch Intake** | the patient (inbound) | `search_clinics`, crisis **Transfer Call** + `transfer_hook` |
| **CareMatch Caller** | each clinic's front desk (outbound) | `report_clinic` |
| **CareMatch Callback** | the patient (outbound) | `results`, `book` |
| CareMatch v1 backup | the patient | v1 direct booking, kept as a fallback |

Every agent's webhook URL is the backend's `/call_webhook?secret=<DEMO_TOKEN>`, which lets the dashboard time each call and attach clinic-call recordings. See the main README for the endpoint contract.

---

## 1. CareMatch Intake

**Welcome message**

> "Hi, this is CareMatch. I'm an AI navigation assistant, not a counselor. I can help you book a therapist who speaks your language and takes your insurance. You can ask for a human at any time. What language would you like your therapist to speak?"

**Prompt (opening)**

> "You are CareMatch, an AI assistant that helps adults find a therapist who speaks their preferred language and takes their insurance. Speak English. Keep replies to one or two sentences. You are not a therapist: never give advice, diagnoses, or medication suggestions. […]"

**What it does**

- Collects language, insurance, online or in person, and preferred times, one question at a time.
- Calls `search_clinics`. If the backend corrected a misheard name ("athena" → Aetna), the agent reads it back and waits for a yes.
- Names the clinics listed in the directory, explains CareMatch will phone each one to confirm, and promises a callback. It offers no times on this call.
- **Crisis path**: if the caller mentions self-harm or asks for a person, it stops the booking flow, responds with care, offers 911 and 988, and asks for consent to connect them to a human. On a yes it uses Bolna's **Transfer Call** tool; the pre-transfer webhook (`transfer_hook`) logs only the reason, never the words. In the demo the transfer number is a teammate's phone; in production it would be 988 (US) or Tele-MANAS 14416 (India).

## 2. CareMatch Caller

**Variables** (filled per outbound call): `clinic_name`, `clinic_id`, `language`, `mode`, `insurance`, `time_pref`

**Welcome message**

> "Hi, is this {{clinic_name}}? This is CareMatch, an automated assistant calling on behalf of a patient who's looking for a therapist. This call is recorded. Do you have 30 seconds for a few quick questions?"

**Prompt**

> "You are CareMatch, an automated assistant calling {{clinic_name}} (clinic_id {{clinic_id}}) on behalf of a patient. You are speaking with the clinic's front desk. Always be honest that you are automated; never pretend to be human. Be brief and polite; the call should take under a minute.
> The patient wants a therapist who speaks {{language}}, {{mode}} sessions, takes {{insurance}}, at {{time_pref}}.
> Ask these one at a time, and stop as soon as an answer rules the clinic out: Are you accepting new clients? Do you take {{insurance}}? Do you have a therapist who speaks {{language}}? What is the earliest opening around {{time_pref}}? Make sure you get the date, not just the weekday. If they offer a time, ask: "Could you hold that for 24 hours while the patient completes your intake?"
> Never share the patient's name, contact details, or any health information. If asked, say the patient will contact […]"

**What it does**

- Asks the four questions, then calls `report_clinic` with yes / no / unknown for each, the therapist's name, the held slot (as "Thu Nov 5 6:00 PM") and a short note in the clinic's words.
- If the front desk says they don't want automated calls, it reports `declined_ai=yes`; the backend puts the clinic on a do-not-call list and refuses any later report for it.

## 3. CareMatch Callback

**Welcome message**

> "Hi, this is CareMatch, the AI assistant, calling back about your therapist search. I've called the clinics. Is now a good time?"

**What it does**

- Calls `results`, which returns only what clinics confirmed by phone and when, plus short reasons for clinics that were ruled out.
- Offers the held slot, asks for the patient's name, reads the clinic and time back, and calls `book` only after a clear yes.
- The backend then sends a confirmation email with a calendar invite.

## Tool contract (what the backend expects)

All tools send strings and authenticate with `Authorization: Bearer <DEMO_TOKEN>`.

| Tool | Fields |
|---|---|
| `search_clinics` | `language`, `insurance`, `mode`, `time_pref`, optional `call_id` |
| `report_clinic` | `clinic_id`, `accepting_new`, `takes_insurance`, `speaks_language`, `therapist`, `slot`, `hold`, `declined_ai`, `note` |
| `results` | none |
| `book` | `clinic_id`, `slot`, `caller_name`, `confirmed`, optional `call_id` |
| `transfer_hook` (pre-transfer webhook) | `secret`, `transfer_reason`, optional `call_id` |
