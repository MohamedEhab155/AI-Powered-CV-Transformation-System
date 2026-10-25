# parser.py
import json
import os
import re

import cohere
from PDF_Reader import read_pdf

from dotenv import load_dotenv
load_dotenv()


MODEL1 = "command-a-plus-05-2026"
MODEL2="command-a-03-2025"

SYSTEM_PROMPT = """You convert CV/resume text into structured JSON.

STRICT RULES:
1. Use section titles EXACTLY as they appear in the original CV. Do not rename,
   translate, merge, split, reorder, or add sections.
2. Keep sections in the same order as the original.
3. Do not invent information. If a field is missing, use null (or [] for lists).
4. Keep the original wording of all texts. Do not summarize or rewrite.
5. The text comes from a PDF extraction, so columns may be mixed. Use context
   to put each piece of text in the correct section.
6. Output ONLY valid JSON, with no explanations.

JSON SHAPE:
{
  "name": string | null,
  "job_title": string | null,
  "contact": {
    "email": string | null,
    "phone": string | null,
    "location": string | null,
    "links": [string]
  },
  "sections": [
    {
      "title": string,
      "items": [
        {
          "heading": string | null,      // job title, degree, skill group, ...
          "subheading": string | null,   // company, university, ...
          "date": string | null,
          "location": string | null,
          "text": string | null,         // paragraph text, if any
          "bullets": [string]
        }
      ]
    }
  ]
}

For plain-text sections (e.g. a summary), use one item with only "text".
For simple lists (e.g. skills, languages), use one item with "bullets",
or one item per line if each has a label (put the label in "heading")."""


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()



def _get_text(response) -> str:
    """Return only the final answer text, skipping thinking blocks."""
    parts = []
    for item in response.message.content or []:
        if getattr(item, "type", None) == "text" and getattr(item, "text", None):
            parts.append(item.text)
    if not parts:
        raise ValueError("The model returned no text content (only thinking).")
    return "".join(parts).strip()

def parse_cv(cv_text: str, api_key: str | None = None) -> dict:
    api_key = api_key or os.getenv("api_key_cohere")
    if not api_key:
        raise RuntimeError("Set the COHERE_API_KEY environment variable.")

    co = cohere.ClientV2(api_key=api_key)

    response = co.chat(
        model=MODEL2,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"CV TEXT:\n\n{cv_text}"},
        ],
    )

    raw = _get_text(response)    # Safety: remove markdown fences if the model adds them
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {e}\n\n{raw[:500]}")

   
    return data


def _validate(data: dict, cv_text: str) -> None:
    """Basic checks. Warns if a section title is not found in the original text."""
    if "sections" not in data or not isinstance(data["sections"], list):
        raise ValueError("JSON has no 'sections' list.")

    original = _normalize(cv_text)
    for section in data["sections"]:
        title = section.get("title")
        if not title:
            raise ValueError("A section has no title.")
        if _normalize(title) not in original:
            print(f"WARNING: title '{title}' was not found in the original text.")
        section.setdefault("items", [])


if __name__ == "__main__":

    pdf = read_pdf(r"D:\dtc\cv porject\dtc_cv.pdf")
    cv = parse_cv(pdf["text"])

    out = "output/cv.json"
    os.makedirs("output", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cv, f, ensure_ascii=False, indent=2)

    print(f"Saved {out}")
    print("Sections found:")
    for s in cv["sections"]:
        print(f"  - {s['title']} ({len(s['items'])} items)")