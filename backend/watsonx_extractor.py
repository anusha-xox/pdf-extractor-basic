"""
watsonx_extractor.py
Sends PDF page images to a WatsonX vision model and parses structured
debit memo fields from the response.
"""

import json
import os
import re

from ibm_watsonx_ai import Credentials
from ibm_watsonx_ai.foundation_models import ModelInference


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are a document data-extraction assistant.
Examine the debit memo image and return ONLY a valid JSON object — no extra text.

Extract these fields (use null if a field is not found):
{
  "debit_memo_number":   "<string>",
  "debit_memo_date":     "<YYYY-MM-DD>",
  "vendor_name":         "<string>",
  "vendor_id":           "<string>",
  "po_number":           "<string>",
  "invoice_number":      "<string>",
  "invoice_date":        "<YYYY-MM-DD>",
  "currency":            "<string>",
  "subtotal":            <number or null>,
  "tax":                 <number or null>,
  "total_amount":        <number or null>,
  "reason":              "<string>",
  "line_items": [
    {
      "line_no":         <integer or null>,
      "description":     "<string>",
      "quantity":        <number or null>,
      "unit_price":      <number or null>,
      "line_total":      <number or null>
    }
  ]
}"""


# ---------------------------------------------------------------------------
# WatsonX client (lazy-initialised)
# ---------------------------------------------------------------------------

_model: ModelInference | None = None


def _get_model() -> ModelInference:
    global _model
    if _model is None:
        api_key    = os.environ.get("WATSONX_API_KEY", "")
        project_id = os.environ.get("WATSONX_PROJECT_ID", "")
        model_id   = os.environ.get("WATSONX_MODEL_ID", "ibm/granite-vision-3-2-2b")
        url        = os.environ.get("WATSONX_URL", "https://us-south.ml.cloud.ibm.com")

        if not api_key or not project_id:
            raise EnvironmentError(
                "WATSONX_API_KEY and WATSONX_PROJECT_ID must be set as environment variables."
            )

        credentials = Credentials(api_key=api_key, url=url)
        # Build outside the try so a failed init doesn't cache a broken object
        model = ModelInference(
            model_id=model_id,
            credentials=credentials,
            project_id=project_id,
        )
        _model = model
    return _model


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _parse_json_from_response(text: str) -> dict:
    """Extract the first JSON object found in *text*."""
    # Strip markdown code fences and surrounding whitespace
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: find the first {...} block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        raise ValueError(f"No valid JSON found in model response:\n{text}")


def extract_fields_from_image(base64_image: str) -> dict:
    """
    Send a single base64-encoded page image to the WatsonX vision model
    and return the parsed debit-memo fields as a dict.
    """
    model = _get_model()

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": EXTRACTION_PROMPT,
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64_image}"
                    },
                },
            ],
        }
    ]

    response = model.chat(
        messages=messages,
        params={
            "max_new_tokens": 1024,
            "temperature": 0.1,   # 0 can stall some vision models
        },
    )

    # Safely navigate response — log full response if structure is unexpected
    try:
        raw_text = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected WatsonX response structure: {response}") from exc

    return _parse_json_from_response(raw_text)


def extract_from_pdf_images(page_images: list[str]) -> dict:
    """
    Run extraction on each page image and merge results, preferring
    non-null values encountered on earlier pages. Line items are
    accumulated across all pages.
    """
    if not page_images:
        raise ValueError("No page images provided — PDF may be empty or corrupt.")

    merged: dict = {}
    all_line_items: list[dict] = []

    for page_b64 in page_images:
        fields = extract_fields_from_image(page_b64)
        # Guard: model may return "line_items": null
        line_items = fields.pop("line_items", None) or []
        all_line_items.extend(line_items)

        for key, value in fields.items():
            if merged.get(key) is None and value is not None:
                merged[key] = value

    merged["line_items"] = all_line_items
    return merged
