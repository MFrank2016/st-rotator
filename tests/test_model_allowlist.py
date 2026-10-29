"""模型白名单（``model_filter.available``）：配置解析、客户端判定与网关拦截。

网关用例复用 ``_ProxyServerTestCase``（真实 ThreadingHTTPServer + http.client），
``StRotator`` 用真对象但 ``chat`` / ``request`` 换成假实现，绝不访问网络。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from st_rotator.client import StRotator
from st_rotator.config import (
    AccountConfig,
    Config,
    ConfigStore,
    ModelFilterConfig,
    RateControlConfig,
)

from tests.test_proxy_console_auth import TOKEN, _ProxyServerTestCase

DEFAULT_MODEL = "default-model"


def _raw_config(**extra: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "base_url": "http://127.0.0.1:9/v1",
        "default_model": DEFAULT_MODEL,
        "accounts": [{"name": "a", "api_keys": ["k-0123456789abcdef"]}],
    }
    raw.update(extra)
    return raw


def _rotator(available: list[str]) -> StRotator:
    config = Config(
        accounts=[AccountConfig(name="a", api_keys=["k-0123456789abcdef"])],
        rate_control=RateControlConfig(mode="off"),
        model_filter=ModelFilterConfig(available=available),
    )
    return StRotator(config, logger=lambda _line: None)


class ModelFilterConfigTest(unittest.TestCase):
    def test_legacy_hidden_key_still_loads(self) -> None:
        # 旧配置的 hidden（黑名单）允许存在但被忽略，不能因此启动失败
        flt = ModelFilterConfig.from_dict({"hidden": ["deepseek-v4.1-*"]})
        self.assertEqual(flt.available, [])

    def test_available_parsed_and_deduped(self) -> None:
        flt = ModelFilterConfig.from_dict({"available": [" a ", "a", "b"]})
        self.assertEqual(flt.available, ["a", "b"])

    def test_empty_available_allows_everything(self) -> None:
        self.assertTrue(ModelFilterConfig().is_available("whatever"))

    def test_non_empty_available_blocks_others(self) -> None:
        flt = ModelFilterConfig(available=["a"])
        self.assertTrue(flt.is_available("a"))
        self.assertFalse(flt.is_available("b"))


class ModelBlockReasonTest(unittest.TestCase):
    def test_empty_whitelist_allows_any_model(self) -> None:
        self.assertEqual(_rotator([]).model_block_reason("anything"), "")

    def test_model_outside_whitelist_blocked(self) -> None:
        reason = _rotator(["allowed-model"]).model_block_reason("other-model")
        self.assertEqual(reason, "模型 other-model 不在可用范围内")

    def test_whitelisted_model_allowed(self) -> None:
        self.assertEqual(
            _rotator(["allowed-model"]).model_block_reason("allowed-model"), ""
        )

    def test_missing_model_falls_back_to_default(self) -> None:
        rotator = _rotator(["allowed-model"])
        # 默认模型始终允许，即使没写进白名单
        self.assertEqual(rotator.model_block_reason(None), "")
        self.assertEqual(rotator.model_block_reason(""), "")

    def test_set_model_filter_dedupes_and_persists(self) -> None:
        rotator = _rotator([])
        rotator.set_model_filter([" a ", "a", "b"])
        self.assertEqual(rotator.config.model_filter.available, ["a", "b"])
        self.assertTrue(rotator.model_filter().is_available("a"))
        self.assertFalse(rotator.model_filter().is_available("c"))


class ModelsCatalogTest(unittest.TestCase):
    def test_blocked_and_restricted_reported(self) -> None:
        rotator = _rotator(["allowed-model"])
        rotator.models = lambda: {  # type: ignore[method-assign]
            "data": [{"id": "allowed-model"}, {"id": "other-model"}]
        }
        result = rotator.available_models()
        self.assertEqual([m["id"] for m in result["models"]], ["allowed-model"])
        self.assertEqual(result["blocked"], ["other-model"])
        self.assertTrue(result["restricted"])

    def test_unrestricted_when_whitelist_empty(self) -> None:
        rotator = _rotator([])
        rotator.models = lambda: {  # type: ignore[method-assign]
            "data": [{"id": "a"}, {"id": "b"}]
        }
        result = rotator.available_models()
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["blocked"], [])
        self.assertFalse(result["restricted"])


class _AllowlistProxyTest(_ProxyServerTestCase):
    """白名单非空：名单外模型 400，名单内正常转发。"""

    def raw_config(self) -> dict[str, Any]:
        return _raw_config(model_filter={"available": ["allowed-model"]})

    def make_rotator(self, config: Config) -> StRotator:
        rotator = StRotator(config, logger=lambda _line: None)
        self.chat_calls: list[str | None] = []
        self.request_calls: list[dict[str, Any]] = []

        def fake_chat(messages, model=None, **kwargs):
            self.chat_calls.append(model)
            return {"choices": [], "usage": {"total_tokens": 1}}

        def fake_request(path, method="GET", json_body=None, headers=None):
            self.request_calls.append(
                {"path": path, "method": method, "json_body": json_body}
            )
            return {
                "object": "list",
                "data": [{"id": "allowed-model"}, {"id": "other-model"}],
            }

        rotator.chat = fake_chat  # type: ignore[method-assign]
        rotator.request = fake_request  # type: ignore[method-assign]
        return rotator

    def _post(self, path: str, body: dict[str, Any]):
        return self.request(
            "POST",
            path,
            body=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
            },
        )

    def test_blocked_model_returns_400(self) -> None:
        resp, body = self._post(
            "/v1/chat/completions",
            {"model": "other-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status, 400)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "model_not_available")
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertIn("other-model", error["message"])
        self.assertEqual(self.chat_calls, [])

    def test_allowed_model_forwarded(self) -> None:
        resp, _ = self._post(
            "/v1/chat/completions",
            {
                "model": "allowed-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.chat_calls, ["allowed-model"])

    def test_missing_model_falls_back_to_default(self) -> None:
        resp, _ = self._post(
            "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.chat_calls, [None])

    def test_passthrough_with_blocked_model_returns_400(self) -> None:
        resp, body = self._post(
            "/v1/embeddings", {"model": "other-model", "input": "x"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "model_not_available")
        self.assertEqual(self.request_calls, [])

    def test_passthrough_without_model_is_not_blocked(self) -> None:
        resp, _ = self._post("/v1/embeddings", {"input": "x"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.request_calls), 1)

    def test_non_string_model_returns_400(self) -> None:
        resp, body = self._post(
            "/v1/chat/completions",
            {"model": 123, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "model_not_available")
        self.assertEqual(self.chat_calls, [])

    def test_models_list_filtered(self) -> None:
        resp, body = self.request(
            "GET", "/v1/models", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            [item["id"] for item in json.loads(body)["data"]], ["allowed-model"]
        )


class _OpenWhitelistProxyTest(_ProxyServerTestCase):
    """白名单为空：名单外模型照常放行。"""

    def raw_config(self) -> dict[str, Any]:
        return _raw_config()

    def make_rotator(self, config: Config) -> StRotator:
        rotator = StRotator(config, logger=lambda _line: None)
        self.chat_calls: list[str | None] = []
        self.request_calls: list[dict[str, Any]] = []

        def fake_chat(messages, model=None, **kwargs):
            self.chat_calls.append(model)
            return {"choices": []}

        def fake_request(path, method="GET", json_body=None, headers=None):
            self.request_calls.append({"path": path, "json_body": json_body})
            return {"object": "list", "data": []}

        rotator.chat = fake_chat  # type: ignore[method-assign]
        rotator.request = fake_request  # type: ignore[method-assign]
        return rotator

    def test_model_outside_empty_whitelist_is_allowed(self) -> None:
        resp, _ = self.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {
                    "model": "anything-goes",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
            },
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.chat_calls, ["anything-goes"])

    def test_non_string_model_returns_400(self) -> None:
        resp, body = self.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {"model": 123, "messages": [{"role": "user", "content": "hi"}]}
            ),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
            },
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "model_not_available")
        self.assertEqual(self.chat_calls, [])


    def test_passthrough_non_string_model_returns_400(self) -> None:
        resp, body = self.request(
            "POST",
            "/v1/embeddings",
            body=json.dumps({"model": 123, "input": "x"}),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
            },
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "model_not_available")
        self.assertEqual(self.request_calls, [])


class ModelFilterPersistenceTest(_ProxyServerTestCase):
    """控制台保存白名单必须落盘，否则重启后静默回退。"""

    def raw_config(self) -> dict[str, Any]:
        return _raw_config(model_filter={"hidden": ["legacy-model"]})

    def make_rotator(self, config: Config) -> StRotator:
        rotator = StRotator(config, logger=lambda _line: None)
        rotator.models = lambda: {"data": []}  # type: ignore[method-assign]
        return rotator

    def test_available_models_saved_to_disk(self) -> None:
        response = self.console.set_available_models(["a", "b"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["available_models"], ["a", "b"])

        reloaded = ConfigStore.load(self.store.path)
        self.assertEqual(reloaded.config.model_filter.available, ["a", "b"])
        raw = json.loads(Path(self.store.path).read_text(encoding="utf-8"))
        self.assertEqual(raw["model_filter"], {"available": ["a", "b"]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
