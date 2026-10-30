"""ejiema (易接码) SMS 平台客户端 + 短信验证码轮询。

平台契约见 docs/ejiema_and_sensenova_api.md §A：全部 GET、参数进 query、
``keyWord`` 等中文参数必须 URL 编码；业务失败返回纯文本 ``ERROR:<msg>``
（HTTP 仍 200）。本模块只负责传输与解析，轮询/号码去重等决策在上层。

只用标准库 urllib.request，风格对齐 quota.HttpQuotaTransport / autorenew.HttpKeyManager。

失败语义：``SmsError``（detail 为原始响应文本或网络/HTTP 摘要）。
"""

from __future__ import annotations

import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Protocol

from .quota import USER_AGENT

# ---------------------------------------------------------------- 常量
ERROR_MARKER = "ERROR:"
# 两端均非数字的 6-8 位连续数字串；re.ASCII 防止全角数字（１２３４５６）被误当成验证码
CODE_RUN = re.compile(r"(?<!\d)\d{6,8}(?!\d)", re.ASCII)


class SmsError(RuntimeError):
    """ejiema API 失败：业务 ERROR 响应或网络/HTTP 层错误。

    仅 HTTP 5xx 与网络层/超时错误可重试（``retryable=True``）；业务 ERROR 与
    解析失败不可重试，调用方须立即按失败处理。
    """

    detail: str
    retryable: bool

    def __init__(self, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable


class SmsTransport(Protocol):
    """SMS 平台传输抽象（供上层 worker / 测试 Fake 实现）。"""

    def left_amount(self) -> float: ...
    def get_phone(self, *, keyword: str) -> str: ...
    def get_msg(self, phone: str, *, keyword: str) -> str: ...


class EjiemaSms:
    """默认实现：stdlib urllib.request 直连 ejiema 平台。"""

    BASE: str = "https://api.ejiema.com/zc/data.php"

    token: str
    base: str
    timeout: float

    def __init__(self, token: str, *, base: str = BASE, timeout: float = 20.0) -> None:
        self.token = token
        self.base = base
        self.timeout = timeout

    # --- 低层 HTTP ---
    def _request(self, action: str, **params: str) -> str:
        """发一次 GET 并返回原始文本；响应含 ``ERROR:`` 子串 → SmsError(原文)。"""
        query = {"code": action, "token": self.token}
        query.update(params)
        url = self.base + "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "text/plain, */*",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - 读不到错误体也不阻塞上报
                text = ""
            finally:
                exc.close()
            raise SmsError(
                f"{action} HTTP {exc.code}: {text}", retryable=exc.code >= 500
            ) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            # socket.timeout / TimeoutError 均为 OSError 子类；IncompleteRead /
            # BadStatusLine 是 HTTPException（不是 OSError），同样归为可重试网络层
            raise SmsError(
                f"{action} {type(exc).__name__}: {exc}", retryable=True
            ) from exc
        if ERROR_MARKER in raw:
            raise SmsError(raw)
        return raw

    # --- 业务方法 ---
    def left_amount(self) -> float:
        """查询余额：解析纯文本 CNY 金额；不可解析 → SmsError。"""
        raw = self._request("leftAmount")
        try:
            return float(raw.strip())
        except ValueError as exc:
            raise SmsError(f"无法解析余额: {raw.strip()!r}") from exc

    def get_phone(self, *, keyword: str = "商汤") -> str:
        """获取一个号码（keyWord 为发件人标签，如【商汤】→ 商汤）。"""
        return self._request("getPhone", keyWord=keyword).strip()

    def get_msg(self, phone: str, *, keyword: str) -> str:
        """拉取该号码的短信原文（可能含 ``[尚未收到]``，由调用方轮询处理）。"""
        return self._request("getMsg", phone=phone, keyWord=keyword)


# ---------------------------------------------------------------- 纯函数
def extract_code(text: str) -> str | None:
    """启发式提取验证码：正文里「最后一个」6-8 位连续数字串（最新验证码）。

    两端必须是非数字（避免从更长的数字串中截取）；无匹配返回 None。
    """
    matches = CODE_RUN.findall(text)
    return matches[-1] if matches else None


def wait_sms_code(
    sms: SmsTransport,
    phone: str,
    *,
    keyword: str,
    interval: float = 5.0,
    timeout: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """轮询短信直到提取到验证码或超时。

    每 ``interval`` 秒调一次 ``sms.get_msg``；提取到验证码立即返回。
    平台抖动（HTTP 5xx / 读超时，``SmsError.retryable``）视为「本次没拿到」继续
    下一轮，不作废整个号码尝试；业务错误（token 错、号码已释放等）立即抛出。
    注入时钟显示的已用时间 >= ``timeout`` 时返回 None。测试注入假 clock /
    sleep，绝不真实阻塞。
    """
    start = clock()
    while True:
        try:
            code = extract_code(sms.get_msg(phone, keyword=keyword))
        except SmsError as exc:
            if not exc.retryable:
                raise  # 业务错误立刻失败，保留原始原因
            code = None  # 抖动按「尚未收到」处理，仍受 timeout 总时长约束
        if code is not None:
            return code
        if clock() - start >= timeout:
            return None
        sleep(interval)
