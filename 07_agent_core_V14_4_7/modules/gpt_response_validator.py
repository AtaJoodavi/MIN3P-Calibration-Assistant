from __future__ import annotations

"""Strict local validation after API Structured Outputs."""

from typing import Any


REQUIRED_FIELDS = {
    "scientific_assessment": str,
    "recommended_group": str,
    "recommended_action": str,
    "suggested_parameter": str,
    "suggested_pair": str,
    "suggested_direction": str,
    "confidence": (int, float),
    "reasoning_summary": str,
    "freeze_suggestions": list,
    "reactivation_suggestions": list,
    "stop_continue_recommendation": str,
    "warnings": list,
}


def supervisor_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scientific_assessment": {"type": "string"},
            "recommended_group": {"type": "string"},
            "recommended_action": {"type": "string"},
            "suggested_parameter": {"type": "string"},
            "suggested_pair": {"type": "string"},
            "suggested_direction": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning_summary": {"type": "string"},
            "freeze_suggestions": {"type": "array", "items": {"type": "string"}},
            "reactivation_suggestions": {"type": "array", "items": {"type": "string"}},
            "stop_continue_recommendation": {"type": "string"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": sorted(REQUIRED_FIELDS),
    }


def validate_gpt_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("GPT supervisor response must be a JSON object.")
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"GPT supervisor response missing fields: {', '.join(missing)}")
    extra = sorted(set(payload).difference(REQUIRED_FIELDS))
    if extra:
        raise ValueError(f"GPT supervisor response has unexpected fields: {', '.join(extra)}")
    for field, type_ in REQUIRED_FIELDS.items():
        if field == "confidence":
            # bool is a subclass of int in Python, but it is not a JSON number
            # acceptable for scientific confidence.
            if type(payload[field]) not in (int, float):
                raise ValueError("GPT supervisor field 'confidence' has invalid type.")
        elif not isinstance(payload[field], type_):
            raise ValueError(f"GPT supervisor field '{field}' has invalid type.")
    confidence = float(payload["confidence"])
    if not 0 <= confidence <= 1:
        raise ValueError("GPT supervisor confidence must be between 0 and 1.")
    return payload
