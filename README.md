# Hindi-English Code-Switching Voice Agent

A LiveKit voice agent that understands and speaks naturally code-switched
Hindi-English, using Rime as the sole TTS engine. See `PERMANENT_RULES.md`
for why Rime is mandatory everywhere in this codebase, and `RIME_EVIDENCE.md`
for the specific hard-voice-problem claim, acceptance test, and results.

## Architecture

```
User mic
  -> Deepgram STT (nova-3, language=multi)
  -> Groq LLM (openai/gpt-oss-120b, code-switch-aware system prompt)
  -> CodeSwitchTTS
       -> language_segmenter.py splits the reply into Hindi/English
          clause-level spans (Devanagari-script check + romanized-Hindi
          marker-word heuristic)
       -> each span synthesized independently via the official
          livekit-plugins-rime client -- one Coda voice per language,
          since no single Coda voice speaks both
       -> spans crossfaded together (60ms linear crossfade) into one
          continuous PCM stream
  -> LiveKit room -> user's speakers
```

`AgentSession` (LiveKit Agents 1.0) handles barge-in natively: when the user
starts talking while the agent is speaking, it stops TTS playback and
cancels the in-flight LLM generation. `turn_manager.py` hooks into the
session's state-change events on top of that so a future tool-call
cancellation layer (not built yet -- see "Known limitations") has turn IDs
to check against.

## Third-party services

| Service | Used for | Plan/tier assumed |
|---|---|---|
| [LiveKit Cloud](https://livekit.io) | WebRTC room/transport, agent worker dispatch | Free tier project |
| [Deepgram](https://deepgram.com) | STT (`nova-3` model) | Pay-as-you-go API key |
| [Groq](https://groq.com) | LLM inference, OpenAI-compatible endpoint | Free/developer API key |
| [Rime](https://rime.ai) | TTS -- the only speech synthesis provider in this project | API key with Coda model access |

## Exact Rime configuration

This is the precise, non-negotiable config per `PERMANENT_RULES.md` --
every spoken-output path in this repo uses this and nothing else.

| Parameter | Value |
|---|---|
| Model ID | `coda` |
| Voices (speaker) | English: `lyra` &nbsp;/&nbsp; Hindi: `nadi` (both Coda voices; overridable via `RIME_VOICE_ENGLISH` / `RIME_VOICE_HINDI` env vars) |
| Language codes | `eng` (English segments) / `hin` (Hindi segments) -- passed per-request since Coda serves 8+ languages |
| Client/transport | Official [`livekit-plugins-rime`](https://docs.livekit.io/reference/python/livekit/plugins/rime/index.html) package (wraps Rime's HTTP TTS endpoint), one client instance per language |
| Audio format | PCM, 24000 Hz, mono, 16-bit |
| Streaming mode | Non-streaming per segment (`TTSCapabilities(streaming=False)`) -- each language segment is fetched as one complete chunk, then crossfaded in-process before being handed to LiveKit's `AudioEmitter` |

Voice IDs were verified against the live catalog at
`https://users.rime.ai/data/voices/all-v2.json` on 2026-09-05. Re-verify
before a demo -- Rime's catalog changes.

## Files

| File | Role |
|---|---|
| `agent/main.py` | Entrypoint. Wires STT/LLM/TTS into an `AgentSession`, defines the code-switch-aware system prompt, hooks turn/barge-in events. |
| `agent/codeswitch_tts.py` | The only TTS path in the project. Segments text by language, synthesizes each span via the official Rime plugin, crossfades the result. |
| `agent/language_segmenter.py` | Splits LLM text into Hindi/English clause-level spans. Pure stdlib, no external dependencies. |
| `agent/turn_manager.py` | Tracks turn IDs for future tool-call cancellation. Stub. |
| `.env.example` | Template for all required environment variables -- placeholders only, no real keys. |
| `requirements.txt` | Python dependencies. |
| `PERMANENT_RULES.md` | Non-negotiable project rule: Rime is the only TTS engine, everywhere, always. |
| `RIME_EVIDENCE.md` | Hard voice problem claim, acceptance test, procedure, and results. |

## Setup instructions

```bash
git clone <this-repo>
cd <this-repo>
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # fill in real LIVEKIT_*, DEEPGRAM_API_KEY,
                                 # GROQ_API_KEY, RIME_API_KEY -- .env is
                                 # gitignored, never commit it
python agent/main.py dev
```

Then connect via LiveKit's
[agents-playground](https://agents-playground.livekit.io) (same LiveKit
Cloud project as your `.env`), join a room, and talk to it. No custom
frontend required to reproduce the demo.

## Known limitations

- **No single Rime voice speaks both Hindi and English.** This project
  works around that by stitching two independent per-language Rime calls
  per reply. This is a deliberate architecture choice, not an oversight.
- **Prosody at the language-switch seam is a known soft spot.** Each
  segment is synthesized as an independent utterance, so Rime has no
  awareness of anything outside its own segment. Mitigations in place:
  non-final segments are forced to end in a comma rather than a period
  (avoids false sentence-final falling intonation mid-reply), the system
  prompt nudges the LLM to switch at clause boundaries, and both voices
  share one `speed_alpha` so pacing doesn't jump. This reduces but does not
  eliminate the artifact -- a fully continuous prosodic contour across a
  language switch is not achievable with two independently synthesized
  voices. `CodeSwitchTTS.last_stitch_report` logs segment count, languages,
  and crossfade duration per utterance for inspection.
- **Coda has no per-word pronunciation override.** Rime's phoneme-bracket
  custom pronunciation feature is Mist v1/v2 only; Coda (required here for
  Hindi voice support) doesn't support it. Mispronunciations on Coda are
  addressed via Rime's `/oov` coverage-check endpoint plus a direct
  dictionary-addition request to Rime, not via in-code phoneme overrides.
- **Segmentation is a heuristic, not a trained classifier.** A ~35-word
  romanized-Hindi marker-word list plus a Devanagari-script check decides
  which voice each clause gets. It can misroute an English proper noun or
  borrowed word sitting inside an otherwise-Hindi clause to the Hindi
  voice. No accuracy benchmark exists yet against a labeled test set.
- **Deepgram Nova-3 `language=multi` has a reported Hindi/Spanish
  misdetection issue** per Deepgram's own community forum. Confirmed in
  our own testing so far to return correct Devanagari-script transcripts
  for Hindi speech, but not yet stress-tested at volume.
- **Tool-call cancellation is not implemented.** `turn_manager.py` tracks
  turn IDs and exposes `is_current()`, but nothing consumes it yet -- no
  tool calls exist in this repo. This is planned as a separate FastAPI
  service.

## Failure behavior

- **Rime unavailable (bad key, outage, quota exceeded):** the request
  raises/surfaces an error through the LiveKit TTS pipeline. There is no
  fallback TTS engine anywhere in this codebase -- per `PERMANENT_RULES.md`,
  a silent fallback to another vendor would invalidate every claim in
  `RIME_EVIDENCE.md`. The failure is visible in agent logs
  (`type='tts_error'` or a `session close` event with a non-null error) and
  should be surfaced as an error state in any production UI built on top of
  this.
- **Deepgram/Groq unavailable:** surfaces the same way -- as a logged error
  event (`type='stt_error'` / `type='llm_error'`) rather than a silent
  no-op. During development we saw this directly: a stale Groq model name
  produced a clear `404 model_not_found` in the logs rather than a silent
  hang.
- **Segmentation produces zero segments** (e.g. empty LLM output):
  `CodeSwitchTTS` returns without calling Rime at all rather than sending
  an empty request.

## Next steps (not yet built)

- FastAPI cancellation-token service for in-flight tool calls, consuming
  `turn_manager.is_current()`.
- Eval harness scoring segmentation accuracy and code-switch seam quality
  against a labeled test set of real code-switched phrases.
- `/oov` coverage check run against demo phrases ahead of any live
  presentation to catch Rime dictionary gaps in advance.

## Resources

- [Rime docs](https://docs.rime.ai)
- [Rime voice catalog](https://users.rime.ai/data/voices/all-v2.json)
- [LiveKit Agents 1.0 docs](https://docs.livekit.io/agents)
- [livekit-plugins-rime reference](https://docs.livekit.io/reference/python/livekit/plugins/rime/index.html)
