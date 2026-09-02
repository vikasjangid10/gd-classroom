# AI GD Classroom

A voice-first, AI-moderated Group Discussion platform.

A **Super User** creates a classroom for a topic (LangChain, MCP, RAG, System Design…)
and picks **up to 4 people registered on the app**. The invitation appears in their
session immediately — pushed over their own event stream, no email, no polling, no
reload. Once at least **two** have accepted the discussion can run; nobody waits on a
seat that stays empty. An **AI host** then runs the whole thing voice-to-voice:

```
microphone → streaming STT → LLM (session memory only) → streaming TTS → all peers
```

The host behaves like a person running the room: it introduces the topic, states the
rules, puts a question to one participant, **listens to the whole answer, takes a beat to
consider it**, then names the point that was actually made and puts the next question to
someone else. It follows up when an answer is thin, keeps speaking time even, and writes
a summary at the end.

You hear it whether or not you have a microphone — every sentence is synthesised once and
delivered both as WebRTC audio and as a clip the browser can play. **All conversation
state lives in process memory and is destroyed when the session ends**, the audio
included. No vector database, no long-term memory.

Full technical design: [`docs/architecture.html`](docs/architecture.html)

---

## Stack

| Layer | Choice |
|---|---|
| Frontend | React 18 · TypeScript · TailwindCSS · Vite |
| Backend | FastAPI · SQLAlchemy 2.0 (async) · Alembic |
| Database | PostgreSQL 16 |
| Transports | REST (commands) · SSE (server→client events) · WebRTC (audio only) |
| Invitations | In-app over the lobby SSE stream · optional SMTP magic links |
| Voice | `aiortc` SFU · pluggable streaming STT / LLM / TTS providers |

Deliberately **not** used: Redis, Kafka, RabbitMQ, vector DBs, Elasticsearch, microservices.

---

## Quick start

**Windows** — one command, and it handles the traps for you:

```powershell
.\run.ps1
```

It starts Docker Desktop if it is not running, brings the stack up, waits for the API to
report healthy, and prints where to go and who to sign in as.

| | |
|---|---|
| `.\run.ps1` | start everything |
| `.\run.ps1 status` | what is running, and which AI providers are live |
| `.\run.ps1 reset` | close discussions nobody ended — **the fix for "already in another live discussion"** |
| `.\run.ps1 test` | the whole flow end to end, no browser needed |
| `.\run.ps1 browser-test` | five real browsers, screenshots in `frontend/e2e/shots/` |
| `.\run.ps1 logs` | follow the API log |
| `.\run.ps1 stop` | stop (the database and the Piper voice are kept) |

**Anywhere else**, or by hand:

```bash
cp .env.example .env
docker compose up -d --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| API docs | http://localhost:8000/docs |
| Readiness | http://localhost:8000/api/v1/readyz |
| Postgres | `localhost:55432` — **not** 5432, so it cannot collide with a local one |

The database is migrated and seeded automatically on first boot, and the Piper voice
(~60 MB) is fetched once into a named volume.

> **After changing `.env`, recreate rather than restart.** Compose only reads `env_file`
> when a container is *created*, so `docker compose restart api` keeps serving the old
> settings and the change looks like it did nothing. Use
> `docker compose up -d --force-recreate api` — which is what `.\run.ps1` does every time.

### Seeded accounts

All seeded users share the password `Password123!`.

| Email | Role |
|---|---|
| `super@gdclassroom.io` | SUPER_USER — the host |
| `priya@gdclassroom.io` | PARTICIPANT |
| `arjun@gdclassroom.io` | PARTICIPANT |
| `meera@gdclassroom.io` | PARTICIPANT |
| `dev@gdclassroom.io` | PARTICIPANT |
| `sana@gdclassroom.io` | PARTICIPANT |
| `rahul@gdclassroom.io` | PARTICIPANT |

Six participants, so a decline has somebody to replace it with. Anyone who registers
through **Sign up** appears in the host's picker too.

---

## Try the whole flow

1. Open a few browser profiles (or private windows) and sign in as some participants.
   Leave those tabs open on **Invitations** — nothing is waiting yet.
2. As `super@gdclassroom.io`, pick a topic, **choose four people** from the list, and
   press **Create classroom & invite 4**.
3. Watch the participants' tabs: the invitation banner appears **without a reload**. It
   arrives over each user's own SSE stream, published in the same transaction that
   created the classroom.
4. Accept in **as many as you like — two is enough**. Once everyone invited has answered
   the room opens by itself; or the host can press **Start with 2** (or 3) without
   waiting for the rest. Everyone is offered **Join the discussion**, again with no
   polling.
5. In the room, press **Join with microphone**, or **Join without a microphone** if you
   have no mic — you will hear the host either way. If the browser blocks autoplay, a
   *Turn on sound* bar appears; one click and it plays.
6. The host can open the same room to listen in, and is the only one who can end it.

If somebody declines, the host's classroom page offers a dropdown of the remaining
registered participants to fill the empty seat, and a **re-invite** control on any
invitation that is still pending or has expired.

### Nobody is named in the round

Inside a discussion everyone is given a **different name** — *Harsh*, *Leher*, *Pooja*,
*Zaid* — never the one on their account. A group discussion is judged on what people say,
and a real name carries things a listener can be swayed by without meaning to be.

The moderator never learns the real name at all: the live session is built from these
names, so there is nothing in its context to repeat. The same names appear in the live
transcript, the stored transcript and the summary. They are derived from the session and
the seat, so they survive a reload with nothing stored, and they are shuffled per session
— you are not the same person twice, and the order does not reveal who accepted first.

**Gender is declared at sign-up, never guessed from your name.** It decides which pool
your name comes from, and nothing else. Leaving it as *Prefer not to say* is a normal
choice: those seats draw from the whole pool.

The host's **classroom page** still shows who they invited and who accepted — that is
administration, not the discussion. Inside the room the host sees the discussion names
too, which means the host cannot currently tell who said what; if you need that for
grading, it is a deliberate feature to add rather than a leak to leave in.

### Sharing contact details ends your round

The host states it in the ground rules, and it is enforced: anyone who reads out an
**email address or phone number** is removed from the discussion on the spot, their
microphone is disconnected, and reconnecting does not put them back.

What they said is **dropped, not stored** — it never reaches the transcript, the
database, the summary, or anyone else's screen. Only the kind of detail is reported
(`"kinds": ["phone"]`), never the value. Live captions are redacted at source, because
they are published seconds before the final transcript that triggers the removal.

Detection is deterministic ([`app/domain/privacy.py`](backend/app/domain/privacy.py)), not
a model call. A number counts as personal in one of two ways:

- **Long enough on its own** — eight digits or more: `9876543210`, `+91 98765 43210`,
  `(0120) 456 7890`, card numbers, and the spoken form *"nine eight seven six five…"*.
- **Or introduced as somebody's** — *"my file number is 63771346"*, *"my roll number
  is 180245"*, aadhaar, PAN. Four digits is enough once the sentence has said whose it
  is; the framing is the signal, not the length.

Email is caught written and spoken (*"priya at gmail dot com"* — how Whisper transcribes
it). Ordinary technical talk is left alone: "up to 90 seconds", "15000 requests a day",
"2400 ms to 180 ms", "in 2024", "chunk size 512", even "my chunk number is 512". Postal
addresses and employers are deliberately **not** detected, because they have no reliable
shape and guessing at them would eject people for saying where they work.

`REMOVE_ON_PERSONAL_INFORMATION=false` keeps the redaction and drops the ejection.

### When it says "already in another live discussion"

A discussion that nobody ended still holds its participants, so they cannot be invited
anywhere else. The janitor sweeps those on its own — after 15 minutes for a room nobody
joined, 90 for one that was running — which is a long time to wait when you are sitting
in front of it. Instead:

```powershell
.\run.ps1 reset          # or: docker compose exec api python -m scripts.reset_rooms
```

Note that ending is asynchronous: the host says goodbye and writes a summary before the
seats are actually released, so give it a few seconds.

Don't want to open five browsers? `docker compose exec api python -m scripts.e2e_demo`
runs the identical flow over HTTP and prints the discussion as it happens.

---

## Making it real

Everything below is optional and independent — the defaults run entirely on this
machine with no keys and no accounts anywhere.

### Real AI

`AI_PROVIDER=fake` runs a scripted moderator with synthetic audio — the whole
orchestration, SSE and WebRTC path, offline. Each port can be switched on by itself.
**This repo's `.env` is currently configured live:**

```env
AI_PROVIDER=live

LLM_BACKEND=openai   OPENAI_API_KEY=sk-...       # the moderator's questions & summary
STT_BACKEND=groq     GROQ_API_KEY=gsk_...        # Whisper, via console.groq.com
TTS_BACKEND=piper                                # local voice — no key, no network
```

Verified end to end, including a round trip that proves both halves of the voice
pipeline: Piper synthesises a sentence, and Groq transcribes it back verbatim.

**Piper needs nothing but disk.** With `TTS_BACKEND=piper` the container downloads a
~60 MB ONNX voice into the `piper` volume on first boot and synthesises the moderator
locally, so you get a real voice without an account anywhere. `STT_BACKEND=deepgram` and
`TTS_BACKEND=elevenlabs` are the hosted alternatives.

Groq serves Whisper as a *file* endpoint rather than a socket, so
[`groq_stt.py`](backend/app/infrastructure/ai/groq_stt.py) does its own utterance
detection: an energy gate raises `speech_started` immediately, `SILENCE_END_MS` of quiet
ends the turn, and `STT_INTERIM_SECONDS` controls how often a live caption is
transcribed (`0` bills only for finals).

### The moderator runs on two fallback chains

Because it does two different jobs, and they want opposite things from a model.

| Router | Job | Wants |
|---|---|---|
| **fast** (`LLM_CHAIN_FAST`) | questions, hand-offs, nudges — everything the room hears | a plain instruct model; every second is dead air |
| **deep** (`LLM_CHAIN_DEEP`) | judging what a participant said, and the closing report | reasoning; nothing it writes is ever spoken |

Each is an ordered list of provider tiers. Every call walks its chain top-down, takes the
first eligible rung, and moves down on failure — **the chain is the retry**, so no
provider is ever tried twice within one call.

```env
LLM_CHAIN_FAST=gemini,groq,huggingface,openai,scripted
LLM_CHAIN_DEEP=groq-strong,gemini-strong,huggingface-strong,openai-strong,scripted
```

Every hosted provider appears in both lanes under two names — `gemini` and
`gemini-strong` — running two different models from one key. **The rung names are per
lane and so are their ledger rows**, deliberately: providers count a free-tier quota per
*model*, so one shared row would bench a healthy rung the moment its sibling ran out.
`ollama` and `scripted` are local, have nothing to make stronger, and are shared.

The two lanes also **lead with different providers**, so one bad day upstream cannot take
out both jobs at once.

All four hosted providers speak OpenAI's chat-completions wire format, so each rung is a
`TierSpec` and no adapter at all — which is why the client in
[`providers.py`](backend/app/infrastructure/llm/providers.py) is written against the
protocol rather than a vendor SDK.

The chain must **end on a local tier**, and the boot refuses one that does not: without
it there is no rung with no quota and no network in front of it, and a bad day upstream
becomes an outage in front of people waiting to speak. `scripted` is the offline
moderator this project already ships; `ollama` is there if you run a local model.

A rung with no key stays in the chain, marked `UNAVAILABLE` with the name of the variable
it wants. Dropping it silently would make "no key configured" and "healthy but never
needed" look identical on the status page.

#### Reasoning models cannot serve this rung, and say so with a 200

The moderator writes one sentence and gets a 220-token budget for it. A reasoning model
spends that budget thinking and returns `content: ""` with `finish_reason: length` —
**HTTP 200**. Taken at face value that is a moderator who takes their turn and says
nothing, in a room where the only symptom is silence, and a chain that records it as a
success and never moves down.

Measured on the real moderator prompt, before the defaults were corrected:

| Model | Result |
|---|---|
| `openai/gpt-oss-20b` at default effort | 218 of 220 tokens spent thinking, **empty answer** |
| `gemini-3.7-flash` | 44 s, answer truncated after four words |
| `gemini-3.6-flash` | 3.8 s, answer truncated mid-sentence |
| `gemini-3.5-flash-lite` | 1.5 s, complete sentence, no hidden tokens |
| `openai/gpt-oss-20b` with `reasoning_effort=low` | 0.6 s, complete sentence |

So: `GEMINI_MODEL` is a **lite** model, `GROQ_REASONING_EFFORT=low` is sent on every Groq
call (a `TierSpec.extra_body`, because a knob one provider has and the others do not is
data like everything else here) — and, underneath both, **an empty answer is a failure**.
The client raises rather than returning `""`, and a stream that ends without a token does
too, so the call moves down the chain instead of producing silence.

A failed rung is benched, and *how long for* depends on why it failed:

| Why | What happens |
|---|---|
| Out of quota for the day | benched until that provider's own midnight |
| Bad key, unknown model, malformed request | benched 30 min — a human has to act |
| Short throttle ("try again in 8s") | **not** benched; still first in line next call |
| A blip | benched only after 3 in a row, on a doubling cooldown; any success resets it |

**Only a spent quota moves the chain down for good.** Everything else is temporary: a
throttle costs one call, a blip costs three before it costs anything, and in both cases
the top rung is first in line again immediately.

> **Measured, on a real 429.** Gemini's free tier for `gemini-3.5-flash` is **20 requests
> per day** — and the error it sends says *"Please retry in 32.394904523s"*. Read by the
> duration heuristic alone that is a per-minute throttle, and the spent rung goes back to
> the top of the chain on every call until midnight, paying a round trip each time. What
> catches it is the quota id in the same body:
> `GenerateRequestsPerDayPerProjectPerModel-FreeTier`. A **named window beats a stated
> duration**, always.

Distinguishing a daily quota from a per-minute throttle is therefore the expensive call
in both directions, and no provider marks it machine-readably. Gemini in particular sends
the *same sentence* for both — "You exceeded your current quota" — and differs only in
the quota id it attaches (`...PerDayPerProjectPerModel` versus `...PerMinute...`). So the
classifier reads a named window first, falls back to generic exhaustion wording only when
no per-minute marker is present, and otherwise uses the *duration* of the retry-after —
parsed from headers, structured fields, and prose (Groq says *"Please try again in
44m44.448s"*). Over five minutes is a day; under is a moment.

The ledger persists to a volume, so a spent tier is not re-probed after every deploy.

#### Judging an answer, without ever making the room wait

The deep lane reads each contribution and returns a small verdict — substance 0-5,
whether it engaged with what came before, whether a follow-up would genuinely add
something, and one clause for the closing report. That verdict decides the *last*
question in [`turn_policy.py`](backend/app/domain/turn_policy.py): whether to come back
to the speaker. It never decides the ones above it — out of follow-ups, out of time —
because those are rules about the round rather than opinions about the words, and a model
that can overrule them turns a good assessment into an unfair discussion.

It is bounded twice: the deep chain's own per-rung ceilings, and
`LLM_ASSESSMENT_TIMEOUT_SECONDS` (6s) at the call site. On timeout, a bad verdict, or
`LLM_ASSESSMENT_ENABLED=false`, the word-count heuristic decides exactly as it did
before — the model is an improvement on it, never a dependency of it. Measured on a real
discussion: **461–776 ms per judgement**, on `openai/gpt-oss-120b`.

#### What it costs, per job

Every billed call writes one `llm.usage` line — tier, model, lane, purpose, and the two
token counts kept apart because they are *priced* apart. That is the one place per-call
noise earns its keep: the monitor records only state transitions, so a chain that is
working perfectly writes nothing, and "what did today cost, and on which job" would
otherwise be unanswerable. The ledger accumulates the same numbers per rung per quota day.

Measured over one complete discussion (19 turns, 8 participant answers):

| Lane | Rung | Purpose | Calls | Tokens in | Tokens out |
|---|---|---|---:|---:|---:|
| fast | `gemini` | moderator utterances | 10 | 8,549 | 320 |
| deep | `groq-strong` | `assess:answer` | 8 | 4,381 | 833 |
| deep | `groq-strong` | `summary:final` | 1 | 1,160 | 336 |
| fast | `gemini` | rolling-summary fold | 2 | 414 | 143 |
| | | **total** | **21** | **14,504** | **1,632** |

**Input outweighs output nine to one**, and the single largest line is the moderator's
speaking prompt at ~850 tokens a call — persona, topic, rolling summary and recent turns.
That is where the context budget in [`prompts.py`](backend/app/domain/prompts.py) is
spent, and the first place to look if it ever needs to be cheaper.

#### Watching both

`GET /api/v1/llm/chain` (host only) reports both lanes, and the **Routers** page in the
app renders it: per-rung status, model, call count and the reason each rung is or is not
serving, plus a shared event feed. The feed is deliberately *not* split per lane — the
two chains run on the same providers and the same keys, so the interleaving is usually
the whole story.

![The routers page](frontend/e2e/shots/llm-routers.png)

### Falling back per port

Any port whose key is missing falls back to the fake **independently** and says so at
WARNING on boot. `ALLOW_TEXT_INPUT=true` lets someone without a microphone take turns by
typing; those produce exactly the commands the STT callbacks produce, so the moderator
cannot tell the difference. Set it to `false` for a genuinely voice-only room.

### Optional: email invitations as well

Off by default — invitations are delivered in the app. Turning it on additionally emails
each invitee a magic join link, for people who are not currently looking at the app, and
lets a host invite someone who has **no account at all** (a passwordless account is
created and the emailed token becomes their credential).

```env
MAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=abcdefghijklmnop     # 16-char App Password, NOT your account password
MAIL_FROM=you@gmail.com
PUBLIC_APP_URL=http://192.168.0.4:5173   # must be reachable from *their* device
```

Gmail needs 2-Step Verification and an App Password from
<https://myaccount.google.com/apppasswords>; a normal password is rejected with a 535,
which the API reports as *"the mail server rejected the credentials"*. With
`MAIL_ENABLED=true` but no `SMTP_HOST`, messages are written to `MAIL_OUTBOX_DIR` as
`.eml` files instead of sent. `GET /api/v1/readyz` reports the active transport
(`off` / `console` / `smtp`).

### Other devices on your network

Only needed if people join from their own phones or laptops. Set all three together to
an address they can reach:

```env
PUBLIC_APP_URL=http://192.168.0.4:5173
VITE_API_BASE_URL=http://192.168.0.4:8000
CORS_ORIGINS=http://192.168.0.4:5173
```

Across the internet you also need TURN (`TURN_URL` / `TURN_USERNAME` / `TURN_PASSWORD`),
or every part of the product works except the audio.

---

## Local development (without Docker)

Requires Python 3.11+, Node 18+ and a PostgreSQL 16 instance.

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --reload

# frontend
cd frontend
npm install
npm run dev
```

## Tests

```bash
# unit + moderator-loop tests — no database, no network, no microphone
docker compose exec api pytest -q

# lint and the architecture contracts (a module may not reach into another's tables)
docker compose exec api ruff check app
docker compose exec api lint-imports

# types
docker compose exec api mypy app

# the whole product over HTTP: create → invite ×4 → accept ×4 → discuss → summary
docker compose exec api python -m scripts.e2e_demo
```

### Browser smoke test

Drives five real browsers — the host and four participants — through the entire flow,
and writes screenshots to `frontend/e2e/shots/`. The four participants are signed in and
idle **before** the classroom exists, and nothing reloads their page: if the invitation
does not push itself into their tab, this fails.

```bash
docker run --rm --shm-size=1g --network gd-classroom_default \
  -v "$PWD/frontend/e2e:/e2e" -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
  mcr.microsoft.com/playwright/python:v1.47.0-jammy \
  sh -c "pip install -q playwright==1.47.0 && python /e2e/ui_smoke.py"
```

## Layout

```
backend/app/
  core/             config, logging, errors, security, response envelope
  db/               engine, base, repository, unit of work
  domain/           pure logic: state machines, turn policy, ledger, prompts, ports
  modules/          identity, classroom, invitation, session,
                    moderation, voice, notification (incl. email templates)
  application/      the use cases that span modules: enrollment, session gateway, mailman
  infrastructure/   adapters: AI (fake, OpenAI, Deepgram, ElevenLabs) and mail (SMTP, console)
  api/              versioned routers, dependencies, middleware, error handlers
  workers/          the janitor
frontend/src/
  lib/  hooks/  pages/  components/  store/  types.ts
```

Three rules are enforced in CI rather than by convention (`lint-imports`):

1. `app/domain` imports nothing from SQLAlchemy, FastAPI or any module.
2. A module may use another module's **service**; never its repository or tables.
3. Modules never import `app/application` — dependencies point inward.

### Resource note

The smoke test launches five Chromium instances. Give the container room:
`--shm-size=1g --memory=3g`, and expect it to need ~3 GB on the Docker host.
