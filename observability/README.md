# Observability with self-hosted Langfuse

PeopleDesk can send a trace for every chat message to [Langfuse](https://langfuse.com). Tracing is
**off** unless the three `LANGFUSE_*` settings below are present, so tests, the golden evaluation
and the demo work the same without it.

## What a trace shows

One trace per chat message (`chat`), tagged with the role and grouped by chat session and user ID:

| Observation | Type | Content |
|---|---|---|
| `route` | chain | The router's structured decision (domain, intent, extracted fields) |
| `llm.router`, `llm.standard`, `llm.complex`, `llm.standard.tools` | generation | Model, prompt messages, reply or tool calls, token usage, latency |
| `leave_agent`, `onboarding_agent`, `parking_agent` | agent | The agent loop: intent, final status, rounds, tool count, pending action |
| `tool.<name>` | tool | Typed tool arguments and result; failed tools are marked WARNING |
| `policy.search` | retriever | The question and the retrieved policy sources |
| `confirmation` | guardrail | The pending action and the user's yes / cancel |

One-time passwords, passwords and bearer tokens are masked before anything is exported, and the
onboarding-approval reply (which contains credentials) is never sent.

## Start Langfuse locally

Requires Docker Desktop (Compose 2.24+). From the repo root:

```bash
./observability/langfuse-up.sh
```

This clones the official Langfuse repository next to this one (`../langfuse-selfhost`) and starts
it with `observability/langfuse.override.yml`, which keeps Langfuse's Postgres off host port 5432
(PeopleDesk uses it) and pre-creates the project and API keys.

- UI: <http://localhost:3000>
- Login: `langfuse-admin@example.com` / `PeopleDesk!Trace-2026`
- Project: **PeopleDesk**

## Connect PeopleDesk

Add to the repo-root `.env`, then recreate the backend:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-peopledesk-local
LANGFUSE_SECRET_KEY=sk-lf-peopledesk-local
LANGFUSE_HOST=http://host.docker.internal:3000
```

```bash
docker compose up -d --build --force-recreate backend
```

`host.docker.internal` lets the backend container reach Langfuse on your machine. When running the
backend outside Docker, use `LANGFUSE_HOST=http://localhost:3000`.

Send a chat message, then open **Tracing** in Langfuse. Traces appear within a few seconds.

## Stop or remove

```bash
cd ../langfuse-selfhost
docker compose -p langfuse stop        # keep data
docker compose -p langfuse down -v     # delete all Langfuse data
```
