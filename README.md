# Voice Agent Skeleton — Component 4

STT (Deepgram) -> LLM (OpenAI, swappable) -> TTS (Rime, mandatory) with
barge-in handling, built on LiveKit Agents.

## Files

- `agent/main.py` — entrypoint. Wires STT/LLM/TTS together, defines the
  code-switch-aware system prompt, and hooks barge-in events to the turn
  manager.
- `agent/rime_tts.py` — Rime TTS adapter (streaming). This is the **only**
  TTS path in the project — see `PERMANENT_RULES.md`.
- `agent/turn_manager.py` — in-process turn/cancellation-token tracking.
  Stub for now; Component 1 (FastAPI cancellation-token tool-call system)
  will extend this to cover in-flight tool calls, not just TTS interrupts.
- `.env.example` — copy to `.env` and fill in real keys.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in LIVEKIT_*, DEEPGRAM_API_KEY, OPENAI_API_KEY, RIME_API_KEY
python agent/main.py dev
```

You'll need a LiveKit room/frontend to connect to it (LiveKit's
`agents-playground` works for quick manual testing without building your own
UI first).

## What's real vs. stubbed

- STT, LLM, and the Rime streaming call are wired for real use — give it
  valid keys and it should produce audio.
- Barge-in stops the current Rime audio and opens a new turn via
  `TurnManager`. This satisfies Problem 2 at the "stop talking" level.
- What's **not** implemented yet: actual tool calls (DB lookups) checking
  `turn_manager.is_current()` before speaking a result — that's Component 1,
  built as a separate FastAPI service this agent will call. Right now
  `TurnManager` only tracks turn IDs; nothing consumes them for tool-call
  cancellation yet.
- Pronunciation overrides (Component 2) aren't applied yet — `rime_tts.py`
  has a comment marking where SSML/phoneme-hint text substitution should
  happen before the payload is built.
- The language-fingerprint calculator (Component 3) has a hook point at
  `_on_user_speech_committed` in `main.py` but isn't computing anything yet.

## Next component

Component 1 (FastAPI cancellation-token tool-call system) is next — it
needs a real tool call to attach `turn_manager.is_current()` checks to, and
it's required for the "Bhai, kal ki flight ka cancellation charge kitna hai"
demo script.
