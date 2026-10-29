"""Shared environment-secret redaction for text and structured records."""
import json
import os
import re


def secret_variants(min_length=1):
    for key, value in os.environ.items():
        if len(value) >= min_length and re.search(r"KEY|TOKEN|PASSWORD|SECRET", key, re.I):
            yield from (value, json.dumps(value, ensure_ascii=False)[1:-1],
                        json.dumps(value, ensure_ascii=True)[1:-1])


def redact(text, min_length=1):
    for variant in secret_variants(min_length):
        text = text.replace(variant, "[REDACTED]")
    return text


def redact_value(value, min_length=1, redact_keys=False):
    if isinstance(value, str):
        return redact(value, min_length)
    if isinstance(value, dict):
        return {(redact(key, min_length) if redact_keys and isinstance(key, str) else key):
                redact_value(item, min_length, redact_keys) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item, min_length, redact_keys) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, min_length, redact_keys) for item in value)
    return value
