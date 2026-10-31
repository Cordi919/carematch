# Bolna agents

CareMatch runs as three agents in Bolna's Agent Studio. All three use Bolna's cascaded voice engine (speech-to-text → LLM → text-to-speech) and call this repo's backend as tools.

| Agent | Talks to | Backend tools |
|---|---|---|
| **CareMatch Intake** | the patient (inbound) | `search_clinics`, crisis Transfer Call + `transfer_hook` |
| **CareMatch Caller** | each clinic's front desk (outbound) | `report_clinic` |
| **CareMatch Callback** | the patient (outbound) | `results`, `book` |

Exported agent configurations (prompts, tools, variables) from Agent Studio will be added to this folder, with API keys, phone numbers and tokens removed. The tool contract each agent calls is in the main [README](../README.md).
