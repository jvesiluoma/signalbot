"""
Ollama LLM client used by both the dashboard summary path and the poller's
per-URL analysis path. Owns the shared concurrency semaphore so neither side
can overwhelm a single-GPU Ollama instance.

Extracted from app.py during Phase 7 of the intel-correctness fix-up.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import requests

import config

logger = logging.getLogger("app_core.ollama")

# Shared concurrency cap. Both the poller (per-URL analysis) and the dashboard
# (group summaries) acquire this semaphore before invoking the model, so the
# slow path can't queue-jump the GPU.
ollama_sem = threading.Semaphore(config.OLLAMA_MAX_CONCURRENCY)


class OllamaClient:
    """Client for Ollama /api/chat with JSON format support."""

    def __init__(self, api_url, model):
        if api_url.endswith('/api/generate'):
            self.api_url = api_url.replace('/api/generate', '/api/chat')
        elif api_url.endswith('/'):
            self.api_url = api_url + 'api/chat'
        else:
            self.api_url = api_url + '/api/chat'
        self.model = model
        self.default_options = {
            "temperature": 0.25,
            "top_p": 0.9,
            "top_k": 40,
            "num_ctx": config.OLLAMA_SUMMARY_NUM_CTX,
            "num_predict": config.OLLAMA_NUM_PREDICT,
            "repeat_penalty": 1.1,
            "seed": 42,
            "stop": ["</messages>"],
            # qwen3:4b-instruct-2507 ships with the reasoning template enabled by
            # default. Disabling thinking here is the difference between a 4096-
            # token budget being burned on `thinking` (and `content=''`) and the
            # model emitting the requested JSON directly. See the
            # "OLLAMA content empty but thinking present" warnings in poller logs.
            "think": False,
        }
        logger.info("OllamaClient initialized: url=%s model=%s num_predict=%d think=False",
                     self.api_url, self.model, config.OLLAMA_NUM_PREDICT)

    def chat_json(self, messages, options=None):
        """Send messages to Ollama /api/chat with JSON format. Returns parsed dict.

        Model + num_ctx/num_predict/think are resolved live from the Settings
        overlay each call (5s-cached); the constructor values are the fallback.
        A disabled/unset summary model short-circuits to an error response."""
        model = self.model
        live_opts = {}
        try:
            import settings as _settings
            if not _settings.ai_enabled():
                return self._error_response("AI features are disabled in Settings")
            m = _settings.summary_model()
            if m is None:
                return self._error_response(
                    "LLM disabled (no summary model configured in Settings)")
            model = m
            live_opts = {
                "num_ctx": _settings.summary_num_ctx(),
                "num_predict": _settings.summary_num_predict(),
                "think": _settings.summary_is_thinking(),
            }
        except Exception:
            # settings unavailable (very early startup / DB down) → fall back
            # to the constructor model + static default_options.
            model = self.model

        final_options = self.default_options.copy()
        final_options.update(live_opts)
        if options:
            final_options.update(options)

        # First pass with the resolved options. Some "instruct" models (notably
        # qwen3:4b-instruct-2507) ship with the reasoning template enabled and
        # ignore `think:false`, emitting their answer into the `thinking` field
        # and leaving `content` empty (done_reason=stop). Previously this bubbled
        # up as an error stub that the queue retried three times per group —
        # ~9 min of GPU time wasted while captions/sentiment starved. We now
        # (1) salvage the JSON the model wrote inside its reasoning, and failing
        # that (2) do exactly one hardened retry (thinking off, stop tokens
        # cleared, an explicit JSON-only nudge) before returning the stub.
        resp = self._post_chat(model, messages, final_options)
        if resp["error"]:
            return self._error_response(resp["error"])
        content = resp["content"]
        thinking = resp["thinking"]

        if not content and thinking:
            logger.warning(
                "OLLAMA content empty but thinking present (%d chars, done_reason=%s). "
                "Attempting salvage + one hardened retry.",
                len(thinking), resp["done_reason"])
            salvaged = _extract_first_json_from_text(thinking)
            if salvaged is not None:
                logger.info("OLLAMA recovered: JSON salvaged from thinking text")
                return self._validate_response_structure(salvaged)

            retry_opts = final_options.copy()
            retry_opts["think"] = False
            retry_opts.pop("stop", None)
            retry_opts["num_predict"] = max(
                int(final_options.get("num_predict", 0) or 0), 8192)
            retry_messages = list(messages) + [{
                "role": "user",
                "content": ("Respond with ONLY the JSON object. Do not include "
                            "any reasoning, analysis, or <think> text."),
            }]
            resp = self._post_chat(model, retry_messages, retry_opts)
            if resp["error"]:
                return self._error_response(resp["error"])
            content = resp["content"]
            if not content and resp["thinking"]:
                salvaged = _extract_first_json_from_text(resp["thinking"])
                if salvaged is not None:
                    logger.info("OLLAMA recovered: JSON salvaged from retry thinking")
                    return self._validate_response_structure(salvaged)

        if not content:
            return self._error_response(
                "No response content from LLM (thinking model exhausted its output "
                "budget on hidden reasoning; salvage and hardened retry both failed)")

        try:
            parsed_json = json.loads(content)
            return self._validate_response_structure(parsed_json)
        except json.JSONDecodeError as e:
            extracted_json = _extract_first_json_from_text(content)
            if extracted_json:
                return self._validate_response_structure(extracted_json)
            return self._error_response(f"Invalid JSON from LLM: {e}")

    def _post_chat(self, model, messages, options):
        """One Ollama /api/chat round-trip under the shared concurrency cap.

        Returns a dict: ``error`` (str|None — set on transport/HTTP failure),
        ``content``, ``thinking``, ``done_reason``. Never raises.
        """
        data = {
            "model": model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": options,
        }
        t0 = time.monotonic()
        try:
            with ollama_sem:
                response = requests.post(
                    self.api_url, json=data,
                    timeout=(config.OLLAMA_CONNECT_TIMEOUT, config.OLLAMA_READ_TIMEOUT)
                )
            dt = time.monotonic() - t0
            logger.info("OLLAMA chat response in %.2fs with HTTP %s", dt, response.status_code)
            if response.status_code != 200:
                error_message = f"HTTP {response.status_code}: {response.text[:500]}"
                logger.error("OLLAMA API error: %s", error_message)
                return {"error": error_message, "content": "",
                        "thinking": "", "done_reason": ""}
            json_resp = response.json()
            if 'eval_count' in json_resp:
                logger.info("OLLAMA tokens: eval=%s prompt=%s",
                            json_resp.get('eval_count', 0),
                            json_resp.get('prompt_eval_count', 0))
            msg = json_resp.get('message', {}) or {}
            message_content = msg.get('content', '') or ''
            thinking_content = msg.get('thinking', '') or ''
            logger.debug("OLLAMA raw content (first 500 chars): %s",
                         message_content[:500] if message_content else '<empty>')
            return {
                "error": None,
                "content": message_content,
                "thinking": thinking_content,
                "done_reason": json_resp.get('done_reason', ''),
            }
        except Exception as e:
            logger.exception("Exception during OLLAMA chat request")
            return {"error": f"Request failed: {e}", "content": "",
                    "thinking": "", "done_reason": ""}

    def _validate_response_structure(self, data):
        if not isinstance(data, dict):
            return self._error_response("Response is not a JSON object")

        if 'topics' not in data:
            data['topics'] = []
        if 'takeaways' not in data:
            data['takeaways'] = []

        if not isinstance(data['topics'], list):
            data['topics'] = []

        validated_topics = []
        text_alternatives = ('title', 'description', 'summary', 'content', 'name')
        for topic in data['topics']:
            if isinstance(topic, dict):
                if 'text' not in topic:
                    for alt in text_alternatives:
                        if alt in topic:
                            topic['text'] = topic.pop(alt)
                            logger.debug("Topic field '%s' mapped to 'text'", alt)
                            break
                if 'emoji' not in topic:
                    topic['emoji'] = '⚫︎'
                if 'text' in topic:
                    validated_topics.append(topic)
                else:
                    logger.debug("Dropping topic with no recognized text field: %s", list(topic.keys()))
            elif isinstance(topic, str):
                validated_topics.append({"emoji": "⚫︎", "text": topic})
        data['topics'] = validated_topics

        if not isinstance(data['takeaways'], list):
            data['takeaways'] = []
        data['takeaways'] = [str(item) for item in data['takeaways'] if item]

        return data

    def _error_response(self, error_message):
        return {
            "topics": [{"emoji": "❌", "text": f"Error generating summary: {error_message}"}],
            "takeaways": ["Please check logs or try refreshing"]
        }


def _extract_first_json_from_text(text):
    """Fallback: find and parse the first JSON object in text."""
    if not text or not isinstance(text, str):
        return None
    start_idx = text.find('{')
    if start_idx == -1:
        return None
    brace_count = 0
    end_idx = -1
    for i, char in enumerate(text[start_idx:], start_idx):
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0:
                end_idx = i
                break
    if end_idx == -1:
        return None
    try:
        return json.loads(text[start_idx:end_idx + 1])
    except json.JSONDecodeError:
        return None


def json_to_markdown(response_data):
    """Convert JSON summary response to markdown."""
    if not isinstance(response_data, dict):
        return "Invalid response format"
    lines = []
    for topic in response_data.get('topics', []):
        if isinstance(topic, dict):
            emoji = topic.get('emoji', '⚫︎')
            text = topic.get('text', '')
            if text:
                lines.append(f"{emoji} {text}")
        elif isinstance(topic, str):
            lines.append(f"⚫︎ {topic}")
    takeaways = response_data.get('takeaways', [])
    if takeaways:
        lines.append("")
        lines.append("🚀 **Key Takeaways:**")
        for takeaway in takeaways:
            if isinstance(takeaway, str) and takeaway.strip():
                lines.append(f"• {takeaway}")
    return "\n".join(lines)


def list_models(timeout=(3, 8)):
    """Query Ollama `/api/tags` for installed model names.

    Returns (sorted_names, error_or_None). Best-effort: never raises — the
    Settings UI falls back to free-text entry when Ollama is unreachable.
    """
    base = config.OLLAMA_API_URL
    for suffix in ("/api/generate", "/api/chat"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = base.rstrip("/")
    try:
        r = requests.get(base + "/api/tags", timeout=timeout)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        models = (r.json() or {}).get("models") or []
        names = sorted({
            m.get("name") for m in models
            if isinstance(m, dict) and m.get("name")
        })
        return names, None
    except Exception as e:
        return [], str(e)
