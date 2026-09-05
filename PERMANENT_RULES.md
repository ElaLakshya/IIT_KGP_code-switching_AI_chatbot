# Permanent Project Rules

These rules are non-negotiable for the duration of this project. Any code,
prototype, or demo that violates them should be treated as broken, even if
it "works."

## 1. Rime is the mandatory TTS engine

- **Every** spoken-output code path — main pipeline, fallback paths, error
  messages, debug/test scripts, quick prototypes — must call the Rime API
  for text-to-speech.
- No browser TTS (e.g. `window.speechSynthesis`), no OS-level TTS, no other
  vendor (ElevenLabs, Azure, Google TTS, etc.), not even "temporarily" or
  "just to test the pipeline."
- If Rime is unavailable (outage, missing key, quota), the correct behavior
  is to **surface the failure** (log it, show an error state in the UI), not
  to silently fall back to a different TTS engine. A silent fallback would
  invalidate every pronunciation/latency claim in RIME_EVIDENCE.md.
- This applies to all four scaffolded components: the LiveKit agent, the
  SSML override map, any regression/evaluation harness, and any ad-hoc
  scripts used to sanity-check audio output.

## Why this matters

The project's acceptance tests and headline claims (natural Hindi+English
phonetics, low latency, pronunciation accuracy) are specifically claims
*about Rime*. If any part of the system can be traced to another TTS engine,
the evidence collected is no longer evidence for the thing being claimed.
