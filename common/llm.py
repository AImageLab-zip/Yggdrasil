"""Calling an OpenAI-compatible chat endpoint.

Transport only: what to send and how to survive the answer. *What* to ask is
``common/llm_tasks.py``, and *where* to ask is ``common/external_config.py``.

Built on ``requests``, which is already pinned for the runner's job API. Adding ``httpx``
or the ``openai`` SDK would mean a new dependency, an image rebuild and another package
to keep current, for an endpoint whose whole surface is one POST.

The service being reached is whatever an admin pointed the row at. OpenRouter by default,
but the same code talks to a self-hosted vLLM or Ollama, which is the escape hatch if
sending clinical text to a third party is ruled out.
"""

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

#: How much of an error body to keep. Enough to name the problem, little enough that a
#: provider echoing the prompt back in an error does not put clinical text in the log.
ERROR_BODY_CHARS = 200


class LlmError(Exception):
    """Base for everything this module raises. Never carries the API key."""


class LlmUnavailable(LlmError):
    """No usable service: no row, disabled, or the key variable is empty."""


class LlmTimeout(LlmError):
    """The endpoint did not answer in time."""


class LlmUpstreamError(LlmError):
    """The endpoint answered with an error status."""

    def __init__(self, message, *, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


class LlmMalformedResponse(LlmError):
    """The endpoint answered with something that is not a chat completion."""


class LlmBudgetExhausted(LlmError):
    """The model used its whole token budget and produced no answer.

    Its own diagnosis because the cause and the cure are specific: on a reasoning model
    the thinking is charged against ``max_tokens`` and then discarded, so a budget that
    looks generous next to the expected answer can still leave nothing for the answer.
    Raise the limit or lower ``reasoning_effort``; retrying unchanged will not help.
    """


class ChatResult:
    """One completed call."""

    __slots__ = ("text", "usage", "model", "finish_reason")

    def __init__(self, text, *, usage=None, model="", finish_reason=""):
        self.text = text
        self.usage = usage or {}
        self.model = model
        self.finish_reason = finish_reason


def _headers(service):
    headers = {
        "Authorization": f"Bearer {service.api_key}",
        "Content-Type": "application/json",
    }
    # OpenRouter attributes usage to these and shows them in its dashboard. Both are
    # optional everywhere else, so they are only sent when an admin set them.
    referer = service.parameter("referer", "")
    title = service.parameter("title", "")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def _payload(service, messages, *, stream, temperature, max_tokens):
    payload = {
        "model": service.model_name,
        "messages": messages,
        "stream": bool(stream),
    }
    temperature = service.parameter("temperature") if temperature is None else temperature
    if temperature is not None:
        payload["temperature"] = temperature
    max_tokens = service.parameter("max_tokens") if max_tokens is None else max_tokens
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    top_p = service.parameter("top_p")
    if top_p is not None:
        payload["top_p"] = top_p

    # Reasoning models spend part of max_tokens thinking before they answer, and the
    # thinking is not part of the answer -- it is discarded. Left uncapped, a model can
    # use the whole budget on it and return empty content with finish_reason "length",
    # which looks like "the model said nothing" rather than "the model ran out of room".
    # Filing dictated text under headings needs very little reasoning, so this is asked
    # for explicitly rather than left to the provider's default.
    effort = service.parameter("reasoning_effort")
    if effort:
        payload["reasoning"] = (
            {"enabled": False} if effort == "none" else {"effort": effort}
        )
    return payload


def _timeout(service):
    """``(connect, read)``.

    A tuple, not a float. A bare float is the *per-read* timeout, and a large model that
    thinks for 40 seconds before its first token then fails a 30-second "timeout" that was
    never meant to bound thinking time. This is the single most common way a working
    configuration looks broken.
    """
    return (service.connect_timeout, service.timeout)


def _should_retry(status):
    """Only what a second attempt can plausibly fix.

    Never 400/401/403: those are configuration -- a bad key, a model the account cannot
    reach, a malformed request -- and retrying doubles the log noise, the latency and, on
    a metered endpoint, the bill, to arrive at the same refusal.
    """
    return status == 429 or 500 <= status < 600


def _sleep_for(response, attempt):
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return min(float(header), 10.0)
        except ValueError:
            pass
    return 2.0 * attempt


def _post(service, payload, *, stream):
    """POST with at most one retry. Raises an ``LlmError`` subclass, never ``requests``'."""
    url = f"{service.base_url.rstrip('/')}/chat/completions"
    last_error = None

    for attempt in (1, 2):
        started = time.monotonic()
        try:
            response = requests.post(
                url,
                headers=_headers(service),
                json=payload,
                timeout=_timeout(service),
                stream=stream,
            )
        except requests.Timeout as exc:
            last_error = LlmTimeout(f"{service.slug} did not answer in time")
            logger.warning("LLM %s timed out on attempt %s: %s", service.slug, attempt, exc)
        except requests.RequestException as exc:
            last_error = LlmUpstreamError(f"{service.slug} could not be reached")
            logger.warning("LLM %s unreachable on attempt %s: %s", service.slug, attempt, exc)
        else:
            elapsed = time.monotonic() - started
            if response.status_code == 200:
                logger.info(
                    "LLM %s answered in %.1fs (model %s)",
                    service.slug, elapsed, service.model_name,
                )
                return response

            body = (response.text or "")[:ERROR_BODY_CHARS]
            logger.warning(
                "LLM %s returned %s in %.1fs: %s",
                service.slug, response.status_code, elapsed, body,
            )
            last_error = LlmUpstreamError(
                f"{service.slug} returned HTTP {response.status_code}",
                status=response.status_code,
                body=body,
            )
            if not _should_retry(response.status_code):
                raise last_error
            if attempt == 1:
                time.sleep(_sleep_for(response, attempt))
                continue
            raise last_error

        if attempt == 1:
            time.sleep(2.0)
            continue
        raise last_error

    raise last_error  # pragma: no cover - the loop always returns or raises


def chat(service, messages, *, temperature=None, max_tokens=None):
    """One blocking completion."""
    if service is None:
        raise LlmUnavailable("No language model is configured")

    response = _post(
        service,
        _payload(service, messages, stream=False, temperature=temperature,
                 max_tokens=max_tokens),
        stream=False,
    )
    try:
        data = response.json()
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmMalformedResponse(
            f"{service.slug} did not return a chat completion"
        ) from exc

    result = ChatResult(
        text,
        usage=data.get("usage") or {},
        model=data.get("model") or service.model_name,
        finish_reason=choice.get("finish_reason") or "",
    )
    _refuse_empty_answer(service, result)
    return result


def stream_chat(service, messages, *, temperature=None, max_tokens=None):
    """Yield content fragments as they arrive, then one final ``ChatResult``.

    The last item is the ``ChatResult``; everything before it is a ``str``. Callers that
    only want the text can ignore the last item, and callers that need the usage figures
    do not have to reassemble the fragments themselves.

    A stream that dies mid-answer raises rather than returning a truncated result, so a
    caller cannot mistake half a report for a whole one.
    """
    if service is None:
        raise LlmUnavailable("No language model is configured")

    response = _post(
        service,
        _payload(service, messages, stream=True, temperature=temperature,
                 max_tokens=max_tokens),
        stream=True,
    )

    pieces = []
    model = service.model_name
    usage = {}
    finish_reason = ""
    try:
        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            # Some providers interleave ": OPENROUTER PROCESSING" comments to keep the
            # connection warm while the model thinks.
            if raw.startswith(":"):
                continue
            if not raw.startswith("data:"):
                continue
            data = raw[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                parsed = json.loads(data)
            except ValueError:
                continue

            model = parsed.get("model") or model
            if parsed.get("usage"):
                usage = parsed["usage"]
            for choice in parsed.get("choices") or ():
                finish_reason = choice.get("finish_reason") or finish_reason
                fragment = (choice.get("delta") or {}).get("content")
                if fragment:
                    pieces.append(fragment)
                    yield fragment
    except requests.RequestException as exc:
        raise LlmUpstreamError(f"{service.slug} closed the stream early") from exc
    finally:
        response.close()

    result = ChatResult(
        "".join(pieces), usage=usage, model=model, finish_reason=finish_reason
    )
    _refuse_empty_answer(service, result)
    yield result


def _refuse_empty_answer(service, result):
    """Raise when the model returned no answer, saying which kind of nothing it was.

    An empty completion is otherwise indistinguishable from a model that had nothing to
    say, and the commonest cause is the least obvious: a reasoning model spent the whole
    of ``max_tokens`` thinking, and the thinking is discarded. That is a configuration
    problem with a specific fix, so it gets its own message rather than a shrug.
    """
    if result.text.strip():
        return

    reasoning = 0
    details = (result.usage or {}).get("completion_tokens_details") or {}
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens") or 0

    if result.finish_reason == "length" or reasoning:
        raise LlmBudgetExhausted(
            f"{service.slug} used its whole token budget"
            + (f" ({reasoning} of it on reasoning)" if reasoning else "")
            + " and produced no report. Raise 'max_tokens' or lower "
            "'reasoning_effort' on the service."
        )
    raise LlmMalformedResponse(f"{service.slug} returned an empty answer")
