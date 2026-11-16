"""Strict JSON decoding for model outputs and their envelopes."""

import json


def strict_json(raw: str | bytes) -> object:
    """Reject duplicate keys, non-finite constants and trailing output."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
