"""Gateway routing, credential handling, JSON payloads and usage accounting."""

import json
import math
import urllib.error
import os
import re
import urllib.request
import urllib.parse


def estimate_cost(usage, rates):
    """Reasoning tokens already belong to completion_tokens; never add them twice."""
    if (
        not isinstance(usage, dict)
        or not rates
        or not all(
            type(usage.get(k)) is int for k in ["prompt_tokens", "completion_tokens"]
        )
    ):
        return None
    details = usage.get("prompt_tokens_details") or {}
    if (
        not isinstance(details, dict)
        or usage["prompt_tokens"] < 0
        or usage["completion_tokens"] < 0
    ):
        return None
    cached = details.get("cached_tokens", 0)
    if not isinstance(cached, int) or not 0 <= cached <= usage["prompt_tokens"]:
        return None
    return (
        (usage["prompt_tokens"] - cached) * rates["input"]
        + cached * rates["cached_input"]
        + usage["completion_tokens"] * rates["output"]
    ) / 1_000_000


def make_payload(model, prompt, case, max_tokens, effort, schema=None):
    value = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(case, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": max_tokens,
        "store": False,
    }
    if schema is not None:
        value["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "note_output", "strict": True, "schema": schema},
        }
    if effort is not None:
        value["reasoning_effort"] = effort
    return value


class SetupError(ValueError):
    """Only program-authored, credential-free messages may be displayed."""


def resolve_key(config):
    if config.get("provider") == "nyu_gateway":
        return os.environ.get("AZURE_OPENAI_API_KEY") or config.get("azure_api_key", "")
    return os.environ.get("OPENAI_API_KEY") or config.get("api_key", "")


def model_base_url(config, model):
    if config.get("provider") != "nyu_gateway":
        return config["base_url"]
    version = model.get("model_version")
    if not isinstance(version, str) or not re.fullmatch(
        r"[A-Za-z0-9._-]+/v[0-9.]+", version
    ):
        raise SetupError("Configure model_version, for example gpt-5.4-mini/v1.0.0")
    base = os.environ.get("LLM_API_BASE_URL") or config["base_url"]
    return base.rstrip("/") + "/" + version


def check_live_config(config, key):
    if config.get("provider", "openai") not in ["openai", "nyu_gateway"]:
        raise SetupError("provider must be openai or nyu_gateway")
    url = urllib.parse.urlparse(config["base_url"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise SetupError(
            "API base URL must be HTTPS without credentials, query or fragment"
        )
    if not key or not key.strip() or key.strip() in ["YOUR_KEY_HERE", "REPLACE_ME"]:
        raise SetupError(
            "Set AZURE_OPENAI_API_KEY for nyu_gateway, or OPENAI_API_KEY for openai; configure_key.py also supports local configuration"
        )
    if config.get("data_approved_for_endpoint") is not True:
        raise SetupError(
            "data_approved_for_endpoint is false in config.local.json. Use data approved for the configured endpoint; set this flag true only after that is confirmed. No API request was sent."
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_completion(base_url, payload, key, gateway=False):
    headers = {
        "Authorization": "Bearer " + key.strip(),
        "Content-Type": "application/json",
    }
    if gateway:
        headers["api-key"] = key.strip()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers=headers,
    )
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=180) as response:
        return json.load(response), response.headers.get("x-request-id")


def decode_response(response):
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "invalid_response", None
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        return "truncated", None
    if choice.get("finish_reason") != "stop":
        return "incomplete_or_filtered", None
    try:
        return "parsed", json.loads(choice["message"]["content"])
    except (KeyError, TypeError, ValueError):
        return "invalid_json", None


def perform_request(payload, limit, rates, send, validate_response):
    try:
        response, request_id = send(payload)
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            delay = float((error.headers or {}).get("Retry-After", "60"))
            delay = max(1, min(delay, 900)) if math.isfinite(delay) else 60
        except (TypeError, ValueError):
            delay = 60
        error.close()
        result = {"http_status": code}
        if code == 429:
            result.update(
                retry_after_seconds=delay,
                reserved_usd=(
                    limit * rates["input"]
                    + payload["max_completion_tokens"] * rates["output"]
                )
                / 1_000_000,
            )
            return "rate_limited", result
        if 500 <= code < 600:
            result["reserved_usd"] = (
                limit * rates["input"]
                + payload["max_completion_tokens"] * rates["output"]
            ) / 1_000_000
            return "gateway_server_error", result
        return "http_error", result
    except (OSError, ValueError) as error:
        return "request_outcome_unknown", {
            "error_type": type(error).__name__,
            "reserved_usd": (
                limit * rates["input"]
                + payload["max_completion_tokens"] * rates["output"]
            )
            / 1_000_000,
        }
    result = {
        "request_id": request_id,
        "raw_response": response,
        "usage": response.get("usage") if isinstance(response, dict) else None,
    }
    return validate_response(result), result
