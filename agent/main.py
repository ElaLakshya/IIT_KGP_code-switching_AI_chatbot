"""
LiveKit voice agent: STT -> LLM -> Rime TTS, with barge-in handling.

Pipeline:
    User audio -> Deepgram STT -> LLM (code-switch aware prompt) -> Rime TTS -> User

Built on LiveKit Agents 1.0's AgentSession/Agent API. The framework itself
handles the mechanics of barge-in (stopping TTS playback, cancelling the
in-flight LLM generation) when `allow_interruptions=True` -- we don't call
`.interrupt()` manually. What we DO hook manually is `turn_manager`: it
needs to know a barge-in happened so that in-flight tool calls (Component 1,
not built yet) can check `is_current()` before speaking a stale result.

NOTE: This is a skeleton meant to run end-to-end with real credentials, not a
finished product. Fill in TODOs before demoing.
"""

import logging
import os

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit.plugins import deepgram, openai, silero

from codeswitch_tts import CodeSwitchTTS
from turn_manager import TurnManager

load_dotenv()
logger = logging.getLogger("voice-agent")
logging.basicConfig(level=logging.DEBUG)

# Adapted from Rime's own prompting guide (docs.rime.ai/docs/prompting),
# which applies to Coda: write for the ear, short sentences, punctuation for
# prosody, no SSML (Coda doesn't accept it), spell() only for IDs. Layered
# on top of that is our code-switching guidance: the LLM should produce
# CLAUSE-level language switches, not mid-word mixing, because our TTS
# stitches separate Hindi/English voice calls at clause boundaries (see
# codeswitch_tts.py) -- mid-clause switches are where the seam sounds worst.
SYSTEM_PROMPT = """You are a bilingual (Hindi-English) voice assistant.
Your reply is spoken aloud by a TTS engine, not read as text. Follow these rules.

CODE-SWITCHING
- The user may code-switch freely between Hindi and English, including
  mid-sentence. Understand the full mixed-language meaning; never ask them
  to repeat themselves in a single language.
- Preserve the user's conversational register in your reply. If they used
  Hindi for emotional/casual phrasing and English for a technical noun
  (e.g. "cancellation charge"), keep that same mixture rather than fully
  translating to one language.
- Only switch your response language wholesale if the user's utterance was
  overwhelmingly in one language. A single Hindi interjection ("arre yaar")
  inside an English sentence is emphasis, not a request to switch languages.
- IMPORTANT: when you do switch languages, switch at a clause boundary
  (comma, period, "and", "but") -- never mid-word or mid-phrase. Your text
  is split at these boundaries and each language is synthesized separately,
  so a clean boundary sounds natural and a mid-clause switch sounds broken.
  Prefer "Bhai, yeh order abhi tak nahi aaya. Can you check the tracking?"
  over "Bhai, order abhi tak nahi delivered hua."
- CRITICAL: if the switch happens in the MIDDLE of your reply (not at the
  very end), end that clause with a COMMA, not a period, even if it reads
  as a complete sentence. Each language segment is synthesized as its own
  independent piece of audio, so a period makes it sound like your whole
  reply just ended right there -- which is wrong and sounds jarring. Only
  use a period on the clause that is genuinely the last thing you say.
  Prefer "Bhai, yeh order abhi tak nahi aaya, can you check the tracking?"
  over "Bhai, yeh order abhi tak nahi aaya. Can you check the tracking?"
  when both clauses are part of the same reply.

SOUND LIKE A PERSON
- Be conversational, not literary. Use contractions. Include light
  disfluencies where natural ("um", "well", "so") -- sprinkle, don't stack.
- Punctuation is your only prosody tool: commas for short pauses, periods
  for falling pitch, question marks for rising intonation, ellipses for a
  trailing pause (use sparingly). Do NOT use SSML tags or markup of any kind.
- Keep sentences under 25 words, ideally under 15. Long sentences without
  internal commas sound breathless.
- Keep a calm, even baseline; save exclamation marks for moments that
  actually warrant them.
- For identifiers that must be read letter-by-letter (OTPs, confirmation
  codes, order IDs), wrap them as spell(ABC123XYZ).
"""


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # --- STT: Deepgram Nova-3, code-switching enabled ---
    # Nova-2 does NOT support Hindi-English code-switching -- Nova-3 is
    # required for real-time Hindi<->English codeswitch transcription.
    # `language="multi"` lets it auto-detect/code-mix rather than forcing
    # one language flag.
    # KNOWN RISK (see project notes): Deepgram has confirmed Nova-3 multi
    # sometimes misdetects Hindi as Spanish. Also verify whether output
    # comes back as Devanagari or Latin-script "Hinglish" for your test
    # phrases -- this affects whether language_segmenter.py needs a
    # transliteration step upstream of it. Neither has been benchmarked yet.
    stt = deepgram.STT(
        model="nova-3",
        language="multi",
        interim_results=True,
        smart_format=True,
    )

    # --- LLM: Groq via OpenAI-compatible endpoint. GROQ_API_KEY / GROQ_MODEL
    # come from .env. Swap base_url/model again if you move providers. ---
    model = openai.LLM(
        model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
        base_url="https://api.groq.com/openai/v1",
        api_key=os.environ["GROQ_API_KEY"],
    )

    # --- TTS: Rime is mandatory (see PERMANENT_RULES.md). Do not swap this. ---
    # CodeSwitchTTS segments text by language and stitches per-language Rime
    # voice calls (see codeswitch_tts.py) because no single Rime voice speaks
    # both Hindi and English. Uses two Coda voices under the hood, read from
    # RIME_MODEL / RIME_VOICE_ENGLISH / RIME_VOICE_HINDI in .env.
    tts = CodeSwitchTTS()

    # VAD drives both end-of-turn detection and barge-in detection.
    vad = silero.VAD.load()

    turn_manager = TurnManager()

    session = AgentSession(
        stt=stt,
        llm=model,
        tts=tts,
        vad=vad,
        allow_interruptions=True,
        min_interruption_duration=0.5,  # min speech length to count as a real barge-in
    )

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev):
        """AgentSession's built-in states: idle/listening/thinking/speaking.
        We use transitions into/out of 'speaking' as our turn boundary,
        replacing the old on("agent_started_speaking") / on("agent_stopped_speaking")
        hooks from the 0.x API."""
        if ev.new_state == "speaking":
            turn_manager.start_new_turn()
        elif ev.old_state == "speaking":
            # Surface the per-utterance stitch report (segment count,
            # languages, crossfade info) for the debug panel / evidence
            # harness, whether this utterance ended naturally or was cut
            # off by a barge-in.
            report = tts.last_stitch_report
            if report:
                logger.info("Utterance stitch report: %s", report)

    @session.on("user_state_changed")
    def _on_user_state_changed(ev):
        """Fires when VAD detects the user starting/stopping speech. If the
        user starts speaking while the agent is mid-response, AgentSession
        itself stops TTS playback and cancels the LLM generation (that's
        what allow_interruptions=True buys us) -- we don't call .interrupt()
        ourselves. What we still need to do by hand is tell turn_manager a
        barge-in happened, since tool-call cancellation (Component 1, not
        built yet) will key off turn IDs, not off AgentSession's internal
        state."""
        if ev.new_state == "speaking" and session.current_speech is not None:
            logger.info("Barge-in detected: cancelling turn")
            turn_manager.cancel_current_turn(reason="user_barge_in")

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev):
        if ev.item.role == "user":
            # This is where the language-fingerprint calculator (Component 3)
            # will hook in: feed ev.item.text_content through the ratio
            # calculator here.
            logger.info("User said: %s", ev.item.text_content)

    @session.on("close")
    def _on_close(ev):
        if ev.error is not None:
            logger.error("Session closed due to error: %s", ev.error)

    await session.start(
        room=ctx.room,
        agent=Agent(instructions=SYSTEM_PROMPT),
    )

    await session.generate_reply(
        instructions='Greet the user with exactly this line, unchanged: '
                     '"Hi! Boliye, kya help chahiye?"'
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))