"""请求体清洗：把商汤网关会误判 400 的写法规范成语义等价的安全写法。

单元用例直接调 ``_normalize_messages``；网关用例起真实 ``ThreadingHTTPServer``，
用假 ``chat`` 记录上游实收内容，绝不访问网络。
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest import mock

from st_rotator import proxy as proxy_mod
from st_rotator.keypool import user_context
from st_rotator.proxy import _normalize_messages, create_server

TOKEN = "testtoken123456"


class NormalizeMessagesTest(unittest.TestCase):
    """六条清洗规则的单元用例（异常输入必须原样返回，且不抛异常）。"""

    def test_assistant_content_null_without_tool_calls_filled(self) -> None:
        fixed = _normalize_messages([{"role": "assistant", "content": None}])
        self.assertEqual(fixed, [{"role": "assistant", "content": ""}])

    def test_assistant_missing_content_filled(self) -> None:
        fixed = _normalize_messages([{"role": "assistant"}])
        self.assertEqual(fixed, [{"role": "assistant", "content": ""}])

    def test_system_and_tool_content_null_filled(self) -> None:
        fixed = _normalize_messages(
            [
                {"role": "system", "content": None},
                {"role": "tool", "content": None, "tool_call_id": "c1"},
                {"role": "tool"},
            ]
        )
        self.assertEqual(
            fixed,
            [
                {"role": "system", "content": ""},
                {"role": "tool", "content": "", "tool_call_id": "c1"},
                {"role": "tool", "content": ""},
            ],
        )

    def test_assistant_with_tool_calls_keeps_null(self) -> None:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ],
        }
        fixed = _normalize_messages([message])
        self.assertIsNone(fixed[0]["content"])

    def test_user_null_and_empty_content_untouched(self) -> None:
        messages = [
            {"role": "user", "content": None},
            {"role": "user", "content": ""},
        ]
        self.assertIs(_normalize_messages(messages), messages)

    def test_numeric_and_bool_content_stringified(self) -> None:
        fixed = _normalize_messages(
            [
                {"role": "user", "content": 123},
                {"role": "user", "content": True},
                {"role": "user", "content": 1.5},
            ]
        )
        self.assertEqual(
            [message["content"] for message in fixed], ["123", "True", "1.5"]
        )

    def test_string_and_multimodal_content_untouched(self) -> None:
        multimodal = [{"type": "text", "text": "hi"}]
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": multimodal},
        ]
        self.assertIs(_normalize_messages(messages), messages)

    def test_developer_role_mapped_to_system(self) -> None:
        fixed = _normalize_messages([{"role": "developer", "content": "x"}])
        self.assertEqual(fixed, [{"role": "system", "content": "x"}])

    def test_developer_with_null_content_becomes_system_empty(self) -> None:
        fixed = _normalize_messages([{"role": "developer", "content": None}])
        self.assertEqual(fixed, [{"role": "system", "content": ""}])

    def test_arguments_object_serialized_to_json_string(self) -> None:
        args = {"a": 1, "b": [1, 2]}
        fixed = _normalize_messages(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"function": {"name": "f", "arguments": args}}],
                }
            ]
        )
        raw = fixed[0]["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(raw, str)
        self.assertEqual(json.loads(raw), args)

    def test_arguments_array_serialized_and_string_untouched(self) -> None:
        fixed = _normalize_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"arguments": [1, 2]}}],
                },
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"arguments": '{"a": 1}'}}],
                },
            ]
        )
        self.assertEqual(
            json.loads(fixed[0]["tool_calls"][0]["function"]["arguments"]), [1, 2]
        )
        self.assertEqual(fixed[1]["tool_calls"][0]["function"]["arguments"], '{"a": 1}')

    def test_malformed_tool_calls_untouched_no_raise(self) -> None:
        messages = [
            {"role": "assistant", "tool_calls": "not-a-list"},
            {"role": "assistant", "tool_calls": [None, "x", {"function": 5}]},
        ]
        self.assertEqual(_normalize_messages(messages), messages)

    def test_non_list_messages_returned_as_is(self) -> None:
        self.assertEqual(_normalize_messages("not-a-list"), "not-a-list")
        self.assertEqual(_normalize_messages(None), None)

    def test_non_dict_message_untouched(self) -> None:
        messages = [{"role": "user", "content": "hi"}, 42, "x", None]
        self.assertIs(_normalize_messages(messages), messages)


class _FakeConfig:
    deadline_seconds = 30.0
    min_max_tokens = 0
    track_stream_usage = False


class _FakePool:
    def user_context(self, label: str):
        return user_context(label)


class _FakeRotator:
    """只提供网关需要的表面；``chat`` 记录上游实收的 messages / 参数。"""

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.pool = _FakePool()
        self.sent: list[tuple] = []

    def model_block_reason(self, model) -> str:
        return ""

    def chat(self, messages, model=None, **kwargs) -> dict:
        self.sent.append((messages, model, kwargs))
        return {"choices": []}


class _GatewayCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rotator = _FakeRotator()
        self.server = create_server(self.rotator, host="127.0.0.1", port=0, token=TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post_chat(self, body) -> tuple[int, bytes]:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=raw,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()


class GatewaySanitizeTest(_GatewayCase):
    def test_unsupported_top_level_fields_removed(self) -> None:
        status, _ = self.post_chat(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "logprobs": True,
                "top_logprobs": 3,
                "metadata": {"a": 1},
            }
        )
        self.assertEqual(status, 200)
        _, _, kwargs = self.rotator.sent[0]
        for field in ("logprobs", "top_logprobs", "metadata"):
            self.assertNotIn(field, kwargs)

    def test_out_of_range_numeric_params_untouched(self) -> None:
        status, _ = self.post_chat(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 3,
                "frequency_penalty": 5,
            }
        )
        self.assertEqual(status, 200)
        _, _, kwargs = self.rotator.sent[0]
        self.assertEqual(kwargs["temperature"], 3)
        self.assertEqual(kwargs["frequency_penalty"], 5)

    def test_clean_request_forwarded_byte_identical(self) -> None:
        body = {
            "model": "m",
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"a": 1}'},
                        }
                    ],
                },
            ],
            "temperature": 0.7,
        }
        status, _ = self.post_chat(body)
        self.assertEqual(status, 200)
        messages, model, kwargs = self.rotator.sent[0]
        kwargs.pop("headers", None)  # 传输层附加的转发头，不属于请求体
        forwarded = {"model": model, "messages": messages, **kwargs}
        self.assertEqual(
            json.dumps(forwarded, ensure_ascii=False).encode("utf-8"),
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )

    def test_bad_messages_shapes_still_forwarded(self) -> None:
        for messages in ("not-a-list", [{"role": "user", "content": "hi"}, 42]):
            with self.subTest(messages=messages):
                status, _ = self.post_chat({"model": "m", "messages": messages})
                self.assertEqual(status, 200)
                self.assertEqual(self.rotator.sent[-1][0], messages)

    def test_sanitize_failure_falls_back_to_original_request(self) -> None:
        messages = [{"role": "assistant", "content": None}]
        with mock.patch.object(
            proxy_mod, "_normalize_messages", side_effect=RuntimeError("boom")
        ):
            status, _ = self.post_chat({"model": "m", "messages": messages})
        self.assertEqual(status, 200)
        self.assertEqual(self.rotator.sent[0][0], messages)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
