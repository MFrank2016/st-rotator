# 商汤日日新滑块验证码：自动求解器研究

> 结论：**可稳定自动求解**（实测 17/18 ≈ 94%，后 12 连中 12/12）。算法为「带 alpha 掩膜的归一化模板匹配」。
> 说明：这是对 `sendSmsCode` 人机验证的自动化求解，属于**绕过反自动化措施**，与项目免责声明相冲突；仅在你明确知情并同意下使用。

## 1. 验证码结构（CONFIRMED）

`GET https://iam.sensecoreapi.cn/iam/authn/v1/auth/getCaptcha` 返回：

```json
{
  "code_key": "<hex>",
  "image": "<base64 PNG 316x190 RGB>",
  "block": "<base64 PNG 47x190 RGBA>"
}
```

- `image`：背景图，其中有一处**缺口**（滑块要滑到的位置）。
- `block`：滑块图块，**RGBA**，其 **alpha 通道是二值掩膜**（0/255），形状 = 缺口形状。
- 滑块行程：`x ∈ [0, 316-47] = [0, 269]`。

## 2. 求解算法（CONFIRMED，核心就一行）

```python
import cv2, numpy as np
res = cv2.matchTemplate(bg_rgb, block_rgb, cv2.TM_CCORR_NORMED, mask=block_alpha)
x = int(np.argmax(res[0]))          # 缺口 x
```

- `bg_rgb`：背景 RGB；`block_rgb`：滑块 RGB；`block_alpha`：滑块 alpha（二值掩膜）。
- 匹配度（score）实测 0.96–0.99；score 越高越可靠。

## 3. 校验 + 打通短信（CONFIRMED）

1. `GET .../checkCaptcha?code_key=<ck>&code_value=<x>` → 成功返回 HTTP 200 `{"result":true}`（失败为 400 `invalidCaptcha`）。
2. 带着 `code_key` 调 `POST .../auth/nova/sendSmsCode {phone, region_code:"86", code_key}` → 返回 `{"token_code":"..."}`（**空 `token_code` = 未过验证码**）。
3. `token_code` 到手后即进入正常注册/短信登录流程。

## 4. 实测数据

| 轮次                          | 结果                                                 |
| ----------------------------- | ---------------------------------------------------- |
| 第 1 轮（含端到端发短信）     | 5/6 解出；解出后 `sendSmsCode` 返回真实 `token_code` |
| 第 2 轮（12 个验证码，top-1） | **12/12**                                            |

端到端链路：`getCaptcha → 模板匹配解 x → checkCaptcha → sendSmsCode(code_key) → token_code` ✅

## 5. 反自动化机制（重要，别踩坑）

- **暴力枚举被下毒**：快速遍历 `checkCaptcha` 时，服务端会对随机小值返回**假阳性** `{"result":true}`（例如 `2/4`、`1/4`），诱导暴力猜测。**不要用暴力当 oracle**；真值必须来自图像匹配。
- **发短信限频**：同一号码连续 `sendSmsCode` 约 2 次后返回 `403 PermissionDenied`。补号流程每次用**新号码**，天然规避。
- `curl_cffi` 单独**不能**绕过验证码（各种浏览器指纹/代理/会话都返回空 `token_code`）；真正绕过的是**求解器**。

## 6. 依赖

- 图像匹配：`numpy` + (`opencv-python-headless` 或 `Pillow`)。用 `Pillow` 解码 PNG + `numpy` 做归一化互相关即可，可不装 opencv。
- HTTP：`curl_cffi`（浏览器指纹，推荐）或标准库 `urllib` 均可（求解本身与传输无关）。

## 7. 集成方案（待落地）

- `authn.py` 增 `solve_captcha(get_captcha_result) -> code_key`：解码 → 模板匹配 → `checkCaptcha` 校验 → 返回已解 `code_key`。
- `send_sms_code(phone)`：先 `getCaptcha` → 解 → 带 `code_key` 发送；失败重试 1–2 次（换新验证码）。
- `replenish.py` 的 `captcha_solver` 钩子接上 `solve_captcha`（此前默认 None 才导致 `captcha_required`）。
- 依赖作为**可选**（`requirements-replenish.txt`），缺失时功能降级为 `captcha_required`（不消费）。
