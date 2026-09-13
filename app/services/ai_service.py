"""Gemini note generation and YouTube transcript fetching.

The prompt is the product here. The old project's prompt was tuned over many
iterations and is carried across deliberately rather than rewritten — the
difference between good notes and mediocre ones is almost entirely in it.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import types

from app.core.config import settings
from app.core.exceptions import AppError

# flash handles lecture-length audio well and is far cheaper than pro. The old
# project used all three; start here and escalate only if quality demands it.
# The name itself lives in settings — see `gemini_model` for why.

MAX_TITLE_LENGTH = 300


class AIError(AppError):
    code = "ai_error"
    message = "Could not generate notes"


class AINotConfiguredError(AIError):
    code = "ai_not_configured"
    message = "GEMINI_API_KEY is not set on the server"


NOTE_PROMPT = """You are an expert academic note-taker. Turn the following
lecture material into structured, comprehensive study notes.

Rules:
- Write in GitHub-flavoured Markdown.
- Open with a single `#` title that names the topic. Do not write "Lecture
  Notes" or repeat the module name.
- Organise with `##` sections that follow the lecture's own structure.
- Preserve every definition, formula, worked example and cited source.
- Write formulas as plain text with Unicode characters: `H₂O`, `CO₂`,
  `6CO₂ + 6H₂O → C₆H₁₂O₆ + 6O₂`, `40°C`, `x²`, `ΔH`. Never use LaTeX or `$`
  delimiters — the clients render Markdown only, so `$\text{H}_2O$` reaches the
  student as that literal string.
- Where the material describes a process, relationship or hierarchy, add a
  Mermaid diagram in a ```mermaid fenced block. Mermaid is strict and a single
  syntax error replaces the diagram with an error box, so keep to this subset:
  - Start with `flowchart TD` (or `LR`). One statement per line.
  - A statement is one link: `A[Label] --> B[Label]`, optionally labelled
    `A[Label] -->|text| B[Label]`. Nothing else on the line.
  - Never join nodes with `+`, `,`, `&` or prose. Two inputs to one step are two
    separate lines.
  - Node ids are letters and digits only. Labels go in `[square brackets]`.
  - Never put `(`, `)`, `{`, `}`, `[`, `]`, `"`, `:`, `;`, `?`, `#` or `-->`
    inside a label. Write `Water H2O`, not `Water (H2O)`.
  - Use plain ASCII inside diagrams — no subscripts, arrows or Greek letters.
  - Prefer one simple diagram over a large one. Omit it if unsure.
- Prefer the lecturer's own wording for definitions; paraphrase explanations.
- Mark anything the speaker flagged as important, examinable or a common
  mistake with a `> **Note:**` blockquote.
- Do not invent content. If the audio is unclear, write `[inaudible]`.
- End with a `## Summary` of the key takeaways as a bullet list.

Return only the Markdown. No preamble, no closing commentary."""


@dataclass(frozen=True)
class GeneratedNote:
    title: str
    markdown: str


def _api_keys() -> list[str]:
    """Primary key first, then the backup.

    The old project added a second key because a single free-tier key hits its
    rate limit during a busy lecture day.
    """
    keys = [settings.gemini_api_key, settings.gemini_api_key_backup]
    return [key for key in keys if key]


def _extract_title(markdown: str, fallback: str) -> str:
    """Use the generated `#` heading as the note's title."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if title:
                return title[:MAX_TITLE_LENGTH]
    return fallback[:MAX_TITLE_LENGTH]


async def _generate(parts: list[types.Part], fallback_title: str) -> GeneratedNote:
    """Call Gemini, falling back to the backup key on failure."""
    keys = _api_keys()
    if not keys:
        raise AINotConfiguredError

    last_error: Exception | None = None

    for key in keys:
        try:
            client = genai.Client(api_key=key)
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=settings.gemini_model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(system_instruction=NOTE_PROMPT),
            )

            markdown = (response.text or "").strip()
            if not markdown:
                raise AIError("The model returned an empty response")

            return GeneratedNote(
                title=_extract_title(markdown, fallback_title),
                markdown=markdown,
            )
        except AIError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure should try the backup key
            last_error = exc
            continue

    raise AIError(f"Note generation failed: {last_error}")


async def generate_from_text(text: str, *, fallback_title: str) -> GeneratedNote:
    return await _generate([types.Part.from_text(text=text)], fallback_title)


async def generate_from_audio(
    data: bytes, *, mime_type: str, fallback_title: str
) -> GeneratedNote:
    return await _generate(
        [
            types.Part.from_bytes(data=data, mime_type=mime_type),
            types.Part.from_text(text="Generate study notes from this lecture recording."),
        ],
        fallback_title,
    )


async def generate_from_pdf(data: bytes, *, fallback_title: str) -> GeneratedNote:
    return await _generate(
        [
            types.Part.from_bytes(data=data, mime_type="application/pdf"),
            types.Part.from_text(text="Generate study notes from this document."),
        ],
        fallback_title,
    )


# ---------------------------------------------------------------------------
# YouTube transcripts
# ---------------------------------------------------------------------------

_YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def extract_youtube_id(url: str) -> str | None:
    match = _YOUTUBE_ID.search(url)
    return match.group(1) if match else None


async def fetch_youtube_transcript(url: str) -> str:
    """Fetch a transcript via Supadata, as the old backend did."""
    if extract_youtube_id(url) is None:
        raise AIError("That does not look like a YouTube video link")

    keys = [settings.supadata_api_key, settings.supadata_api_key_backup]
    keys = [key for key in keys if key]
    if not keys:
        raise AINotConfiguredError("SUPADATA_API_KEY is not set on the server")

    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=60) as client:
        for key in keys:
            try:
                response = await client.get(
                    "https://api.supadata.ai/v1/youtube/transcript",
                    params={"url": url, "text": "true"},
                    headers={"x-api-key": key},
                )
                response.raise_for_status()
                payload = response.json()

                content = payload.get("content")
                if isinstance(content, list):
                    # Segment form: join the individual caption lines.
                    content = " ".join(
                        str(segment.get("text", ""))
                        for segment in content
                        if isinstance(segment, dict)
                    )

                text = str(content or "").strip()
                if not text:
                    raise AIError("That video has no transcript available")
                return text
            except AIError:
                raise
            except Exception as exc:  # noqa: BLE001 - try the backup key
                last_error = exc
                continue

    raise AIError(f"Could not fetch the transcript: {last_error}")
