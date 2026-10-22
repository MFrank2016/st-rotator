"""HttpClient 出口代理测试。

验证：
- 未配置代理时走直连、不调 ``set_tunnel``；
- 配置代理（参数或环境变量）时经代理建连并 CONNECT 隧道到上游；
- 显式 ``proxy=""`` 优先于环境变量；
- 明文 http 上游配代理、非法代理串在构造时报 ``ValueError``。

全程用假连接对象，不发起任何真实网络调用。
"""

import base64
import http.client
import os
import unittest
from unittest import mock

from st_rotator.transport import HttpClient


class TestTransportProxy(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(http.client, "HTTPSConnection")
        self.fake_cls = patcher.start()
        self.addCleanup(patcher.stop)
        self.fake_cls.return_value = mock.MagicMock(name="fake_conn")

    def test_no_proxy_direct_connection(self):
        """不传 proxy 且环境变量未设置 → 直连，不调 set_tunnel。"""
        with mock.patch.dict(
            os.environ, {"HTTPS_PROXY": "", "https_proxy": ""}, clear=False
        ):
            client = HttpClient("https://u.example/v1")
            conn = client._new_connection()
            client.close()

        self.fake_cls.assert_called_once()
        args, kwargs = self.fake_cls.call_args
        self.assertEqual(args, ("u.example", 443))
        self.assertEqual(kwargs["timeout"], client._connect_timeout)
        conn.set_tunnel.assert_not_called()

    def test_proxy_tunnel_with_credentials(self):
        """传非空 proxy → 连代理主机，set_tunnel 指向上游并带 Proxy-Authorization。"""
        client = HttpClient("https://u.example/v1", proxy="http://u:p@proxy.local:2260")
        conn = client._new_connection()
        client.close()

        args, kwargs = self.fake_cls.call_args
        self.assertEqual(args, ("proxy.local", 2260))
        conn.set_tunnel.assert_called_once()
        tunnel_args, tunnel_kwargs = conn.set_tunnel.call_args
        self.assertEqual(tunnel_args, ("u.example", 443))
        expected_b64 = base64.b64encode(b"u:p").decode("ascii")
        self.assertEqual(
            tunnel_kwargs["headers"],
            {"Proxy-Authorization": f"Basic {expected_b64}"},
        )

    def test_empty_proxy_disables_env(self):
        """proxy="" 显式禁用：即使环境里设了 HTTPS_PROXY 仍直连。"""
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://env-proxy.local:1"},
            clear=False,
        ):
            client = HttpClient("https://u.example/v1", proxy="")
            conn = client._new_connection()
            client.close()

        args, _ = self.fake_cls.call_args
        self.assertEqual(args, ("u.example", 443))
        conn.set_tunnel.assert_not_called()

    def test_env_proxy_used_when_param_omitted(self):
        """只设环境变量 HTTPS_PROXY（不传参数）→ 走代理。"""
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://env-proxy.local:2260", "https_proxy": ""},
            clear=False,
        ):
            client = HttpClient("https://u.example/v1")
            conn = client._new_connection()
            client.close()

        args, _ = self.fake_cls.call_args
        self.assertEqual(args, ("env-proxy.local", 2260))
        conn.set_tunnel.assert_called_once()

    def test_plain_http_upstream_rejects_proxy_on_connect(self):
        """显式 proxy + 明文 http 上游 → 构造不报错，首次建连时 ValueError。"""
        client = HttpClient("http://u.example/v1", proxy="http://proxy.local:2260")
        with self.assertRaises(ValueError):
            client._new_connection()
        client.close()

    def test_env_proxy_with_plain_http_upstream_rejects_on_connect(self):
        """环境变量代理 + 明文 http 上游 → 构造不报错，首次建连时 ValueError（不静默直连）。"""
        with mock.patch.object(http.client, "HTTPConnection") as fake_http:
            with mock.patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://env-proxy.local:2260", "https_proxy": ""},
                clear=False,
            ):
                client = HttpClient("http://u.example/v1")
                try:
                    client._new_connection()
                except ValueError:
                    pass
                else:
                    self.fail("代理 + 明文 http 上游应在首次建连时报 ValueError")
                finally:
                    client.close()

        # 报错发生在建连之前：HTTPConnection 不应被调用
        fake_http.assert_not_called()

    def test_invalid_proxy_scheme_rejected(self):
        """非法代理串（如 socks5://）→ 构造时 ValueError。"""
        with self.assertRaises(ValueError):
            HttpClient("https://u.example/v1", proxy="socks5://x:1")

    def test_invalid_proxy_no_credential_leak(self):
        """非法代理串报错时不把密码打进异常文案。"""
        with self.assertRaises(ValueError) as ctx:
            HttpClient("https://u.example/v1", proxy="gopher://secret:pw@host:1")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("pw", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
