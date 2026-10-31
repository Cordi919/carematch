# Bolna agents

CareMatch runs as three agents in Bolna's Agent Studio. All three use Bolna's cascaded voice engine (speech-to-text → LLM → text-to-speech) and call this repo's backend as tools.

| Agent | Talks to | Backend tools | Config |
|---|---|---|---|
| **CareMatch Intake** | the patient (inbound) | `search_clinics`, crisis Transfer Call + `transfer_hook` | [`carematch_intake.json`](carematch_intake.json) |
| **CareMatch Caller** | each clinic's front desk (outbound) | `report_clinic` | [`carematch_caller.json`](carematch_caller.json) |
| **CareMatch Callback** | the patient (outbound) | `get_results` (→ `/results`), `book`, crisis Transfer Call | [`carematch_callback.json`](carematch_callback.json) |

The tool contract each agent calls is in the main [README](../README.md).

## What's in the files

Each file holds one agent as configured in Agent Studio on 2026-10-31: welcome message, prompt, model and voice settings, tool JSON (pasted into **Tools → Custom Function → Write manually**), the crisis transfer, and the webhook. Agent Studio has no export button, so the files were written from the live configuration rather than downloaded.

Secrets and personal details are replaced with placeholders:

| Placeholder | Replace with |
|---|---|
| `<BACKEND_URL>` | the backend's public URL (the demo used an ngrok static domain) |
| `<DEMO_TOKEN>` | the same value as the backend's `DEMO_TOKEN` env var |
| `<TRANSFER_PHONE_NUMBER>` | the human support phone, international format (`+1…`) |

## Shared settings

- LLM: OpenAI `gpt-4.1-mini`, 300 tokens, temperature 0.3
- Voice: Cartesia Sonic-3.5, "Gemma - Decisive Agent"; transcription: Deepgram nova-3
- Language: English. Bolna has no Mandarin voice; the therapist's language is a search parameter, not the call language.
- Webhook (Extractions tab) on all three: `<BACKEND_URL>/call_webhook?secret=<DEMO_TOKEN>`

## Notes from setting these up

- **Caller variables are per call.** The prompt uses `{{clinic_name}}`, `{{clinic_id}}`, `{{language}}`, `{{mode}}`, `{{insurance}}`, `{{time_pref}}`. For test calls, set them in **Agent tab → Variables**, and switch `clinic_name` / `clinic_id` (C001 → C002) between the two clinic calls.
- **Crisis wording is a callback, not a live connection.** On the trial account (India routing), transfers to US numbers did not connect, and web calls can't transfer at all. So the Transfer Call tool says "I'm asking a person from our team to call you back right away", the agent also gives 911 and 988, and the pre-transfer webhook logs "Human callback requested" on the dashboard.
- **`book` field descriptions still mention `search_clinics`** (left over from v1). The Callback prompt tells the agent to copy `clinic_id` and `slot` from `get_results`, which is what it does.
- **Save, then reload before editing again.** A tab that loaded an agent before someone else saved it will overwrite the newer version when you press Save agent. Wait for "Updated a few seconds ago" before switching agents.
- **Tool JSON editor:** the first Submit reformats the JSON; click Submit again to add the tool.
- Text chat tests (**Test options → Chat with agent**) call the real tools and fire the webhook, so they show up on the dashboard like calls.
