# FRICTION.md — the polish ledger

Everything Alex found slow, ugly, confusing, or annoying about CLARVIS, in his words,
one line per item. Say "log friction: …" in chat (or CLARVIS logs it when you complain)
and it lands here. The weekly polish ritual (see POLISH_PROMPT.md) reads this file plus
the real-usage audit log, fixes the top items, and checks them off.

Format: `- [YYYY-MM-DD] the complaint` → when fixed: `- [x] [YYYY-MM-DD] the complaint (fixed YYYY-MM-DD, commit)`

---
- [2026-07-22] Voice: Alex wants full CONVERSATION MODE like the Claude app — press the mic ONCE and talk naturally: it detects when he stops speaking (VAD), sends, speaks the reply, then listens again automatically. Current hold-or-tap flow requires a button interaction per turn. (Design sketch: browser VAD via WebAudio RMS threshold + silence timeout on the existing MediaRecorder loop; interrupt by speaking. ElevenLabs streaming TTS would tighten the reply gap.)
