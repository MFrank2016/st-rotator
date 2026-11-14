"""Anthropic 端点离线单元测试：清洗规则、错误形状、SSE 渲染、请求体解析。

不联网、不消耗额度。端到端契约见 ``tests/test_gateway_contract.py``。
"""

from __future__ import annotations

import json
import unittest

from st_rotator import anthropic


class SanitizeMessageRequestTest(unittest.TestCase):
    """只做语义等价的最小改写，且绝不修改调用方传入的原对象。"""

    def test_null_content_filled(self) -> None:
        payload = {
            "model": "deepseek-v4-flash",
            "max_tokens": 32,
            "messages": [
                {"role": "user", "content": None},
                {"role": "assistant", "content": None},
            ],
        }
        body, changes = anthropic.sanitize_message_request(payload)
        self.assertEqual(body["messages"][0]["content"], "")
        self.assertEqual(body["messages"][1]["content"], "")
        self.assertEqual(len(changes), 2)
        # 原入参不能被改动
        self.assertIsNone(payload["messages"][0]["content"])

    def test_block_array_kept(self) -> None:
        arr = [{"type": "text", "text": "hi"}]
        payload = {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": arr}],
        }
        body, changes = anthropic.sanitize_message_request(payload)
        self.assertEqual(body["messages"][0]["content"], arr)
        self.assertEqual(changes, [])

    def test_empty_block_array_kept(self) -> None:
        payload = {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": []},
            ],
        }
        body, changes = anthropic.sanitize_message_request(payload)
        self.assertEqual(body["messages"][1]["content"], [])
        self.assertEqual(changes, [])

    def test_system_null_filled(self) -> None:
        payload = {
            "model": "m",
            "max_tokens": 8,
            "system": None,
            "messages": [{"role": "user", "content": "hi"}],
        }
        body, changes = anthropic.sanitize_message_request(payload)
        self.assertEqual(body["system"], "")
        self.assertIn("system", changes)

    def test_numeric_strings_converted(self) -> None:
        payload = {
            "model": "m",
            "max_tokens": "64",
            "temperature": "0.7",
            "top_p": "0.9",
            "messages": [{"role": "user", "content": "hi"}],
        }
        body, changes = anthropic.sanitize_message_request(payload)
        self.assertEqual(body["max_tokens"], 64)
        self.assertIsInstance(body["max_tokens"], int)
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["top_p"], 0.9)
        for field in ("max_tokens", "temperature", "top_p"):
            self.assertIn(field, changes)

    def test_unparsable_numeric_kept(self) -> None:
        payload = {
            "model": "m",
            "max_tokens": "abc",
            "messages": [{"role": "user", "content": "hi"}],
        }
        body, _ = anthropic.sanitize_message_request(payload)
        self.assertEqual(body["max_tokens"], "abc")

    def test_legal_fields_untouched(self) -> None:
        payload = {
            "model": "m",
            "max_tokens": 8,
            "top_k": 10,
            "metadata": {"user_id": "u"},
            "messages": [{"role": "user", "content": "hi"}],
        }
        body, changes = anthropic.sanitize_message_request(payload)
        self.assertEqual(body["top_k"], 10)
        self.assertEqual(body["metadata"], {"user_id": "u"})
        self.assertEqual(changes, [])

    def test_non_dict_payload_returned_as_is(self) -> None:
        body, changes = anthropic.sanitize_message_request(None)  # type: ignore[arg-type]
        self.assertIsNone(body)
        self.assertEqual(changes, [])


class ErrorShapeTest(unittest.TestCase):
    def test_error_body_shape(self) -> None:
        err = anthropic.anthropic_error("invalid_request_error", "bad")
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"]["type"], "invalid_request_error")
        self.assertEqual(err["error"]["message"], "bad")

    def test_status_mapping(self) -> None:
        cases = {
            400: "invalid_request_error",
            401: "authentication_error",
            403: "permission_error",
            404: "not_found_error",
            413: "request_too_large",
            429: "rate_limit_error",
            503: "overloaded_error",
            529: "overloaded_error",
            500: "api_error",
            502: "api_error",
        }
        for status, expected in cases.items():
            self.assertEqual(
                anthropic.anthropic_error_type_for(status), expected, status
            )


class UsageAndStreamTest(unittest.TestCase):
    def test_extract_usage(self) -> None:
        result = {
            "type": "message",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 34,
                "cache_read_input_tokens": 5,
            },
        }
        self.assertEqual(anthropic.extract_message_usage(result), (12, 34, 5))

    def test_extract_usage_missing(self) -> None:
        self.assertIsNone(anthropic.extract_message_usage({"type": "message"}))
        self.assertIsNone(anthropic.extract_message_usage(None))

    def test_extract_usage_bad_values(self) -> None:
        self.assertEqual(
            anthropic.extract_message_usage({"usage": {"input_tokens": "x"}}), (0, 0, 0)
        )

    def test_extract_model(self) -> None:
        self.assertEqual(anthropic.extract_message_model({"model": "m"}), "m")
        self.assertIsNone(anthropic.extract_message_model({"model": 1}))

    def test_is_sse_response(self) -> None:
        self.assertIs(anthropic.is_sse_response({"stream": True}), True)
        self.assertIs(anthropic.is_sse_response({"stream": False}), False)
        self.assertIs(anthropic.is_sse_response({"model": "m"}), False)
        self.assertIs(anthropic.is_sse_response(None), False)

    def test_iter_sse_events(self) -> None:
        text = (
            'event: message_start\ndata: {"type":"message_start"}\n\n'
            'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        events = anthropic.iter_sse_events(text)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][0], "message_start")
        self.assertEqual(events[1][0], "message_stop")
        self.assertEqual(json.loads(events[0][1])["type"], "message_start")


class LoadJsonObjectTest(unittest.TestCase):
    def test_valid_object(self) -> None:
        self.assertEqual(anthropic.load_json_object(b'{"model":"m"}'), {"model": "m"})

    def test_invalid_json_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            anthropic.load_json_object(b"not json")

    def test_non_object_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            anthropic.load_json_object(b"[1,2]")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
