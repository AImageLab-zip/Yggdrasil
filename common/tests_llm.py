"""The LLM transport: timeouts, retries, failure shapes, and keeping the key quiet.

No network anywhere -- ``requests.post`` is patched. Three of these tests are here for
reasons that cost real money or real trust if they regress:

* the timeout is a ``(connect, read)`` **tuple**, because a bare float bounds each read
  and makes a slow-but-healthy model look broken;
* a 400/401/403 is **not** retried, because retrying a configuration error doubles the
  latency and the bill to arrive at the same refusal;
* the API key never reaches a log record or an exception string.
"""

import json
import logging
from unittest import mock

import requests
from django.test import SimpleTestCase

from common import external_config, external_services, llm


def service(**overrides):
    fields = {
        "slug": "openrouter_llm",
        "kind": external_services.LLM_CHAT_OPENAI.key,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-v1-TOPSECRETKEY",
        "model_name": "nvidia/nemotron-3-super-120b-a12b:free",
        "parameters": {"temperature": 0.2, "max_tokens": 2000},
        "languages": (),
        "timeout": 60,
        "connect_timeout": 10,
        "ca_cert": "",
        "source": "db",
    }
    fields.update(overrides)
    return external_config.ResolvedService(**fields)


def response(status=200, payload=None, text="", headers=None):
    fake = mock.Mock()
    fake.status_code = status
    fake.headers = headers or {}
    fake.text = text or json.dumps(payload or {})
    fake.json.return_value = payload if payload is not None else {}
    fake.close = mock.Mock()
    return fake


def completion(content="Hello", usage=None, model="a/model"):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage or {"total_tokens": 42},
        "model": model,
    }


class RequestShapeTests(SimpleTestCase):
    def test_the_timeout_is_a_connect_read_tuple(self):
        """A bare float bounds each *read*, not the call.

        A large model that thinks for 40 seconds before its first token then trips a
        30-second "timeout" that was never meant to bound thinking time -- which is the
        most common way a perfectly good configuration looks broken.
        """
        with mock.patch("common.llm.requests.post", return_value=response(payload=completion())) as post:
            llm.chat(service(), [{"role": "user", "content": "hi"}])
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 60))

    def test_it_posts_to_chat_completions_with_the_configured_model(self):
        with mock.patch("common.llm.requests.post", return_value=response(payload=completion())) as post:
            llm.chat(service(), [{"role": "user", "content": "hi"}])
        self.assertEqual(
            post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions"
        )
        self.assertEqual(
            post.call_args.kwargs["json"]["model"],
            "nvidia/nemotron-3-super-120b-a12b:free",
        )

    def test_a_trailing_slash_on_the_base_url_does_not_double_up(self):
        with mock.patch("common.llm.requests.post", return_value=response(payload=completion())) as post:
            llm.chat(service(base_url="https://openrouter.ai/api/v1/"), [])
        self.assertEqual(
            post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions"
        )

    def test_admin_parameters_reach_the_payload(self):
        with mock.patch("common.llm.requests.post", return_value=response(payload=completion())) as post:
            llm.chat(service(parameters={"temperature": 0.9, "max_tokens": 50, "top_p": 0.4}), [])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["temperature"], 0.9)
        self.assertEqual(payload["max_tokens"], 50)
        self.assertEqual(payload["top_p"], 0.4)

    def test_optional_attribution_headers_are_only_sent_when_set(self):
        with mock.patch("common.llm.requests.post", return_value=response(payload=completion())) as post:
            llm.chat(service(), [])
        self.assertNotIn("HTTP-Referer", post.call_args.kwargs["headers"])

        with mock.patch("common.llm.requests.post", return_value=response(payload=completion())) as post:
            llm.chat(service(parameters={"referer": "https://ygg.example", "title": "Y"}), [])
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["HTTP-Referer"], "https://ygg.example")
        self.assertEqual(headers["X-Title"], "Y")


class RetryPolicyTests(SimpleTestCase):
    def test_a_429_is_retried_once(self):
        with mock.patch(
            "common.llm.requests.post",
            side_effect=[response(429, text="slow down"), response(payload=completion())],
        ) as post, mock.patch("common.llm.time.sleep"):
            result = llm.chat(service(), [])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result.text, "Hello")

    def test_a_500_is_retried_once_then_gives_up(self):
        with mock.patch(
            "common.llm.requests.post", return_value=response(500, text="boom")
        ) as post, mock.patch("common.llm.time.sleep"):
            with self.assertRaises(llm.LlmUpstreamError):
                llm.chat(service(), [])
        self.assertEqual(post.call_count, 2)

    def test_a_401_is_not_retried(self):
        """A bad key is not going to be a good key two seconds later."""
        with mock.patch(
            "common.llm.requests.post", return_value=response(401, text="no")
        ) as post, mock.patch("common.llm.time.sleep"):
            with self.assertRaises(llm.LlmUpstreamError):
                llm.chat(service(), [])
        self.assertEqual(post.call_count, 1)

    def test_a_400_is_not_retried(self):
        with mock.patch(
            "common.llm.requests.post", return_value=response(400, text="bad")
        ) as post, mock.patch("common.llm.time.sleep"):
            with self.assertRaises(llm.LlmUpstreamError):
                llm.chat(service(), [])
        self.assertEqual(post.call_count, 1)

    def test_retry_after_is_honoured_and_capped(self):
        with mock.patch(
            "common.llm.requests.post",
            side_effect=[
                response(429, headers={"Retry-After": "600"}),
                response(payload=completion()),
            ],
        ), mock.patch("common.llm.time.sleep") as sleep:
            llm.chat(service(), [])
        self.assertLessEqual(sleep.call_args.args[0], 10.0)

    def test_a_timeout_raises_LlmTimeout(self):
        with mock.patch(
            "common.llm.requests.post", side_effect=requests.Timeout("too slow")
        ), mock.patch("common.llm.time.sleep"):
            with self.assertRaises(llm.LlmTimeout):
                llm.chat(service(), [])

    def test_an_unreachable_host_raises_LlmUpstreamError(self):
        with mock.patch(
            "common.llm.requests.post", side_effect=requests.ConnectionError("no route")
        ), mock.patch("common.llm.time.sleep"):
            with self.assertRaises(llm.LlmUpstreamError):
                llm.chat(service(), [])


class ResponseHandlingTests(SimpleTestCase):
    def test_an_unconfigured_service_raises_LlmUnavailable(self):
        """``None`` is what external_config returns when nothing is usable."""
        with self.assertRaises(llm.LlmUnavailable):
            llm.chat(None, [])

    def test_a_response_without_choices_is_malformed(self):
        with mock.patch("common.llm.requests.post", return_value=response(payload={"id": "x"})):
            with self.assertRaises(llm.LlmMalformedResponse):
                llm.chat(service(), [])

    def test_a_non_json_body_is_malformed(self):
        fake = response()
        fake.json.side_effect = ValueError("not json")
        with mock.patch("common.llm.requests.post", return_value=fake):
            with self.assertRaises(llm.LlmMalformedResponse):
                llm.chat(service(), [])

    def test_usage_and_model_are_captured(self):
        with mock.patch(
            "common.llm.requests.post",
            return_value=response(payload=completion(usage={"total_tokens": 7}, model="m/1")),
        ):
            result = llm.chat(service(), [])
        self.assertEqual(result.usage["total_tokens"], 7)
        self.assertEqual(result.model, "m/1")


class StreamingTests(SimpleTestCase):
    def _stream(self, lines):
        fake = response()
        fake.iter_lines.return_value = iter(lines)
        return fake

    def test_fragments_arrive_then_a_final_result(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"## side\\n"}}]}',
            'data: {"choices":[{"delta":{"content":"left"}}],"model":"m/1"}',
            'data: {"usage":{"total_tokens":11},"choices":[{"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        with mock.patch("common.llm.requests.post", return_value=self._stream(lines)):
            items = list(llm.stream_chat(service(), []))

        fragments = [item for item in items if isinstance(item, str)]
        results = [item for item in items if isinstance(item, llm.ChatResult)]
        self.assertEqual(fragments, ["## side\n", "left"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, "## side\nleft")
        self.assertEqual(results[0].usage["total_tokens"], 11)

    def test_keepalive_comments_and_blank_lines_are_ignored(self):
        """OpenRouter interleaves ": OPENROUTER PROCESSING" while the model thinks."""
        lines = [
            ": OPENROUTER PROCESSING",
            "",
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            "data: [DONE]",
        ]
        with mock.patch("common.llm.requests.post", return_value=self._stream(lines)):
            items = list(llm.stream_chat(service(), []))
        self.assertEqual([i for i in items if isinstance(i, str)], ["x"])

    def test_a_malformed_frame_does_not_abandon_the_stream(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            "data: {not json",
            'data: {"choices":[{"delta":{"content":"b"}}]}',
            "data: [DONE]",
        ]
        with mock.patch("common.llm.requests.post", return_value=self._stream(lines)):
            items = list(llm.stream_chat(service(), []))
        self.assertEqual([i for i in items if isinstance(i, str)], ["a", "b"])

    def test_a_stream_that_dies_raises_rather_than_returning_half_a_report(self):
        """Half a report must never be mistaken for a whole one."""
        fake = response()
        fake.iter_lines.side_effect = requests.ConnectionError("reset")
        with mock.patch("common.llm.requests.post", return_value=fake):
            with self.assertRaises(llm.LlmUpstreamError):
                list(llm.stream_chat(service(), []))

    def test_the_response_is_closed_even_when_the_stream_fails(self):
        fake = response()
        fake.iter_lines.side_effect = requests.ConnectionError("reset")
        with mock.patch("common.llm.requests.post", return_value=fake):
            with self.assertRaises(llm.LlmUpstreamError):
                list(llm.stream_chat(service(), []))
        fake.close.assert_called_once()


class SecrecyTests(SimpleTestCase):
    """The key is sent in a header and appears nowhere else."""

    def test_the_key_is_not_in_any_log_record(self):
        with self.assertLogs("common.llm", level=logging.DEBUG) as logs:
            with mock.patch(
                "common.llm.requests.post",
                return_value=response(payload=completion()),
            ):
                llm.chat(service(), [])
        self.assertNotIn("TOPSECRETKEY", "\n".join(logs.output))

    def test_the_key_is_not_in_a_failure_log_either(self):
        with self.assertLogs("common.llm", level=logging.DEBUG) as logs:
            with mock.patch(
                "common.llm.requests.post",
                return_value=response(401, text="invalid api key sk-or-v1-TOPSECRETKEY"),
            ), mock.patch("common.llm.time.sleep"):
                with self.assertRaises(llm.LlmUpstreamError) as caught:
                    llm.chat(service(), [])
        # The provider echoed the key back in its error body; we log at most 200
        # characters of that body, so assert on what we control: our own message.
        self.assertNotIn("TOPSECRETKEY", str(caught.exception))
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("Bearer", "\n".join(logs.output))

    def test_the_key_travels_only_in_the_authorization_header(self):
        with mock.patch(
            "common.llm.requests.post", return_value=response(payload=completion())
        ) as post:
            llm.chat(service(), [{"role": "user", "content": "hi"}])
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-or-v1-TOPSECRETKEY")
        self.assertNotIn("TOPSECRETKEY", json.dumps(post.call_args.kwargs["json"]))
        self.assertNotIn("TOPSECRETKEY", post.call_args.args[0])


class ReasoningBudgetTests(SimpleTestCase):
    """A reasoning model spends ``max_tokens`` thinking, and the thinking is discarded.

    This is not hypothetical: the first live call of this feature returned nothing at all
    because 1403 reasoning tokens used up a 2000-token budget before the model reached its
    answer. OpenRouter reports that as an ordinary 200 with empty content and
    ``finish_reason: "length"``, which reads as "the model had nothing to say".
    """

    def test_the_reasoning_effort_is_sent(self):
        with mock.patch(
            "common.llm.requests.post", return_value=response(payload=completion())
        ) as post:
            llm.chat(service(parameters={"reasoning_effort": "low"}), [])
        self.assertEqual(
            post.call_args.kwargs["json"]["reasoning"],
            {"effort": "low", "exclude": True},
        )

    def test_effort_none_disables_reasoning_rather_than_asking_for_none_of_it(self):
        with mock.patch(
            "common.llm.requests.post", return_value=response(payload=completion())
        ) as post:
            llm.chat(service(parameters={"reasoning_effort": "none"}), [])
        self.assertEqual(post.call_args.kwargs["json"]["reasoning"], {"enabled": False})

    def test_the_thinking_is_never_returned(self):
        """Excluded, not merely limited.

        A provider may stream its scratchpad as ordinary content -- "We need to extract
        clinical statements and map them to sections..." -- and nothing downstream can
        tell that from an answer, so it lands in the report as if it were dictated.
        """
        with mock.patch(
            "common.llm.requests.post", return_value=response(payload=completion())
        ) as post:
            llm.chat(service(parameters={"reasoning_effort": "medium"}), [])
        self.assertIs(post.call_args.kwargs["json"]["reasoning"]["exclude"], True)

    def test_no_reasoning_parameter_is_sent_when_unset(self):
        with mock.patch(
            "common.llm.requests.post", return_value=response(payload=completion())
        ) as post:
            llm.chat(service(parameters={}), [])
        self.assertNotIn("reasoning", post.call_args.kwargs["json"])

    def test_an_exhausted_budget_is_diagnosed_rather_than_returned_empty(self):
        payload = {
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 1403}},
            "model": "a/model",
        }
        with mock.patch("common.llm.requests.post", return_value=response(payload=payload)):
            with self.assertRaises(llm.LlmBudgetExhausted) as caught:
                llm.chat(service(), [])
        message = str(caught.exception)
        self.assertIn("1403", message)
        self.assertIn("max_tokens", message)

    def test_an_empty_answer_with_no_reasoning_is_merely_malformed(self):
        payload = {
            "choices": [{"message": {"content": "  "}, "finish_reason": "stop"}],
            "usage": {},
            "model": "a/model",
        }
        with mock.patch("common.llm.requests.post", return_value=response(payload=payload)):
            with self.assertRaises(llm.LlmMalformedResponse):
                llm.chat(service(), [])

    def test_a_stream_that_only_reasons_is_diagnosed_too(self):
        """Reasoning arrives as `delta.reasoning`, which is deliberately not content."""
        lines = [
            'data: {"choices":[{"delta":{"reasoning":"Let me think about this..."}}]}',
            'data: {"choices":[{"finish_reason":"length"}],'
            '"usage":{"completion_tokens_details":{"reasoning_tokens":900}}}',
            "data: [DONE]",
        ]
        fake = response()
        fake.iter_lines.return_value = iter(lines)
        with mock.patch("common.llm.requests.post", return_value=fake):
            with self.assertRaises(llm.LlmBudgetExhausted):
                list(llm.stream_chat(service(), []))
