# ejiema (易接码) SMS API + SenseNova (商汤日日新) console/IAM API — full reference

Compiled 2026-10-09 from: official docs raw HTML/JS, live endpoint probes, and the live SenseNova console SPA bundles (`nova-platform-web-console:1.1.0-20260929-40fec6d`).
Legend: **CONFIRMED** = seen in real JS/Hydra/HTTP responses; **INFERRED** = reconstructed, not yet exercised end-to-end.

---

# Section A — ejiema SMS platform API

## A.1 Exact base host (CONFIRMED)

Docs page: <https://www.ejiema.com/api.html>. Rendered HTML hides the host; raw source shows:

```js
$("[domain='domain']").html(window.location.host.replace('www.','').replace('app.','').replace('api.',''));
// source: https://www.ejiema.com/api.html#L202
```

Therefore API host = `api.<site-domain>`. For ejiema.com:

**Base: `https://api.ejiema.com/zc/data.php`** — CONFIRMED live: `GET https://api.ejiema.com/zc/data.php?code=leftAmount` returns `ERROR:缺少参数：token` (HTTP 200, text/plain). `https://www.ejiema.com/zc/data.php` also answers the same.

Global rules (from the docs):
- Encoding UTF-8; method **GET**; all params in query string.
- Params: `code=<action>` always; `token=<API token>` for every action (token is created in the web console, there is NO token API).
- Chinese/special chars in params (e.g. `keyWord`) MUST be URL-encoded.
- **Error format**: plain text `ERROR:<中文错误信息>` on business failure (HTTP still 200).
- **Cost/consumption**: no per-request consumption field is returned. Docs: every 1000 requests (same account/IP) deducts ¥0.01–0.2 from balance; deduction records only in 个人中心 (Personal Center). `queryUsed` is the only API-side history.

## A.2 Endpoints

### leftAmount — query balance
- `GET https://api.ejiema.com/zc/data.php?code=leftAmount&token=<token>`
- Success: plain text balance (CNY amount; format not further fixed by docs). Fail: `ERROR:<msg>`.

### getPhone — get a number
- `GET https://api.ejiema.com/zc/data.php?code=getPhone&token=<token>&keyWord=<URL-encoded>&phone=<optional>&province=<optional>&cardType=<optional>`
- `keyWord` optional, recommended; should be the sender label, e.g. 【毛竹】→ keyWord=毛竹. If omitted you may receive unrelated SMS.
- `phone` optional: return that specific number; omitted → random.
- `province` optional: Chinese province names as in the APP.
- `cardType` optional: `实卡` | `虚卡` | `全部`.
- Success: phone number (plain text). Fail: `ERROR:<msg>`.

### getMsg — fetch SMS for a number
- `GET https://api.ejiema.com/zc/data.php?code=getMsg&token=<token>&phone=<number>&keyWord=<URL-encoded, REQUIRED>`
- `phone` required; `keyWord` required — only SMS containing this string are returned.
- **Not-yet marker**: response containing `[尚未收到]` means SMS not received — poll again.
- Success: full SMS text. Fail: `ERROR:<msg>`.

### release / block — BOTH DOCUMENTED AS AUTO (no call needed)
- `release`: "无需调用，现已实现自动释放。" — numbers auto-release.
- `block`: "无需调用，现已实现自动拉黑重复号码。" — duplicate random numbers auto-blacklisted; explicitly specified numbers are NOT auto-blacklisted.

### send — send SMS from a virtual number
- `GET https://api.ejiema.com/zc/data.php?code=send&token=<token>&phone=<your virtual number>&toPhone=<destination>&projId=<project ID>&content=<text>`
- Success: send result. Fail: `ERROR:<msg>`. Docs warn: cannot send to personal mobile numbers; spam → account ban.

### queryUsed — history
- `GET https://api.ejiema.com/zc/data.php?code=queryUsed&token=<token>`
- **Rate limit: max once per minute, else the account's API access is banned** (only explicit per-endpoint rate limit in the docs).
- Success: history records separated by `\n`; returns last 24h / up to 100 records. Fail: `ERROR:<msg>`.

## A.3 Notes for the client
No maintained OSS client found (GitHub code search misses this class of CN SMS sites); docs below are sufficient. Flow: create token once in web console (`https://www.ejiema.com/appweb/signIn.html` → 个人中心 → API对接中心 → 创建 API Token) → leftAmount → getPhone(URL-encoded keyWord) → poll getMsg until no `[尚未收到]`.

---

# Section B — SenseNova console/IAM API

## B.0 Infrastructure map (all CONFIRMED live)

Per-host config from the SPA auth-sdk (chunk `4039-e42da8a6395288d2.js`, module 56053, "nova-auth-sdk"; also referenced from chunk `254-*/965-*`):

| Host | `sensecoreIamApi` | `signinUrl` | `jwksBaseUrl` | `consoleUrl` | `client_id` |
|---|---|---|---|---|---|
| `platform.sensenova.cn` (public console) | `https://iam.sensecoreapi.cn` | `https://platform.sensenova.cn` | `https://signin.sensecore.cn` | `https://console.sensecore.cn` | `"nova"` |
| `nova.sensecore.tech` / default (dev/tech) | `https://iam.sensecoreapi.tech` | `https://nova.sensecore.tech` | `https://signin.sensecore.tech` | `https://console.sensecore.tech` | `"nova"` |

Live probe results:
- `GET https://iam.sensecoreapi.cn/iam/authn/v1/auth/getCaptcha` → **200** `{"code_key":"…","image":"<base64 PNG>", "block":"<base64 PNG>"}` (works with NO auth).
- `GET https://iam.sensecoreapi.cn/iam/idp/v1/apiKeys` → **401** gRPC `{"code":16,"details":[{"reason":"invalidAuthenticationType"}],"message":"Unauthenticated"}`.
- `GET https://platform.sensenova.cn/lite/console/v1/metered/api-keys` → **401** `{"code":16,...,"error_key":"auth_header_missing","message":"Unauthenticated","request_id":"…"}`.
- `GET https://signin.sensecore.cn/.well-known/jwks.json` → **200** JWKS, `kid:"public:hydra.openid.id-token"`.
- `POST https://platform.sensenova.cn/oauth2/token` (empty body) → **200** Hydra `{"error":"invalid_request","error_description":"The request is missing a required parameter…"}`.
- `POST https://signin.sensecore.cn/oauth2/token` (empty body) → **200** same Hydra error (this host also runs Hydra).
- `GET https://platform.sensenova.cn/oauth2/auth` and `GET https://signin.sensecore.cn/oauth2/auth` → Hydra redirect to `https://iam.sensecoreapi.cn/error?error=invalid_client…` ("Client … does not exist") — i.e. `/oauth2/auth` authorize endpoint exists on both hosts; needs `client_id`.

**Headers for authed calls** (request helper, chunk `4039-…`, module 54511): `Authorization: Bearer <access_token>` + `Accept-Language: zh-CN|en-US` + `Content-Type: application/json`. AuthN (unauth) endpoints are called with `skipBearerAuth: true`, i.e. NO Authorization header.

**Error envelope** (every IAM/lite/authn API): gRPC-style
```json
{"code":3|16, "message":"InvalidArgument|Unauthenticated", "details":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"…","domain":"iam","metadata":{}},{"@type":"type.googleapis.com/google.rpc.LocalizedMessage","locale":"zh-CN|en","message":"…"},{"@type":"type.googleapis.com/sensetime.core.higgs.error_detail.v1.LogInfo","log_id":"…","track_id":"…"}], "request_id":"…", "error_key":"…"}
```
`code 3` = invalid argument (observed for `{}` bodies), `code 16` = unauthenticated.

---

## B.1 Login / auth (password) + OIDC session

### Password login — CONFIRMED
- **Method/URL**: `POST https://iam.sensecoreapi.cn/iam/authn/v1/auth/nova/login`
- **Headers**: `Content-Type: application/json` only (skipBearerAuth).
- **Payload**: `{"username": "<6-24 alnum>", "password": "<8-32 chars>", "challenge": "<login_challenge from /login route>", "is_encrypt": <bool>}` (JWE: when JWKS keys are present the console encrypts the password with `jose` RSA-OAEP + A256GCM using `https://signin.sensecore.cn/.well-known/jwks.json` and sets `is_encrypt: true`; else sends plaintext with `is_encrypt: false`).
- **Response**: `{"redirect": "<url>"}`; console reads `redirect_uri || redirect_to || redirect` and navigates there. If empty → error toast "novaErrGeneric".
- **Source**: chunk `254-d6b62ebd00144e8a.js` module 20035 (`C` fcn) + chunk `965-32a855215515a831.js` login form (password + JWE path).

### OIDC / PKCE session exchange — CONFIRMED (code) + live-confirmed endpoints
- Console flow: SPA keeps `code_verifier`/`state` in localStorage → user lands back with `?code=…` → exchanges:
- **Method/URL**: `POST https://platform.sensenova.cn/oauth2/token` (CONFIRMED live; `{signinUrl}/oauth2/token` in code, signinUrl = platform.sensenova.cn for the public console).
- **Headers**: `Content-Type: application/x-www-form-urlencoded`.
- **Body (form)**: `{code, redirect_uri, code_verifier, state, client_id: "nova", grant_type: "authorization_code"}`.
- **Response**: `{id_token, access_token, …}` → stored in localStorage keys `access_token` / `id_token`; subsequent calls send `Authorization: Bearer <access_token>`.
- **Source**: `layout-ca966232fcc582bf.js` (oauth2 exchange), chunk `4039-…` module 56053 (`client_id="nova"`, localStorage keys).
- OIDC issuer: `https://signin.sensecore.cn` (jwksBaseUrl; `kid "public:hydra.openid.id-token"` = Ory Hydra). `https://signin.sensecore.cn/oauth2/token` ALSO answers as a Hydra token endpoint (live) — but the SPA points the token call at `{signinUrl}` = `platform.sensenova.cn`. The **authorize** URL is `{signinUrl}/oauth2/auth` = `https://platform.sensenova.cn/oauth2/auth?client_id=nova&response_type=code&redirect_uri=…&code_challenge=…&code_challenge_method=S256&state=…` — **INFERRED** params (Hydra standard + PKCE in SPA), but the endpoint existence is CONFIRMED live (returns Hydra `invalid_client` when called bare).
- Correction to the premise in the request: the SPA's token call goes to `platform.sensenova.cn/oauth2/token`, NOT `signin.sensecore.cn/oauth2/token` (signin.sensecore.cn is the JWKS host; it happens to also run Hydra, but it is not what the console uses).

### Multi-step login — CONFIRMED
- `POST https://iam.sensecoreapi.cn/iam/authn/v1/auth/nova/loginNext` body `{"challenge", "username", "user_id", "sign"}` → `{"redirect",…}` (used after tenant selection / intermediate steps). Source: chunk `254-…` (`k` fcn) + chunk `965-…` (tenant picker `E`).

### Login validation rules (from SPA i18n/validation) — CONFIRMED
- username 6–24, `/^[A-Za-z0-9]+$/`; password 8–32, ≥3 of {lower,upper,digit,special}, special = `[~!@#$%^&*?_+.,;:-]`; challenge non-empty else "novaErrChallengeExpired" (challenges expire and are refreshed via `checkChallenge`).

---

## B.2 SMS send + verify

**Two families — note carefully:**

### Family 1: authn (unauthenticated) — for login-by-SMS and registration
- **sendSMS**: `POST https://iam.sensecoreapi.cn/iam/authn/v1/auth/nova/sendSmsCode`
  - Payload: `{"phone": "<digits>", "region_code": "86", "code_key": "<optional captcha code_key>"}` (code_key tied to the solved captcha from getCaptcha when captcha is shown).
  - Response: `{"token_code": "<flow session token>"}` — **this is the "session" for the SMS flow** (60 s resend cooldown in UI).
  - Error for `{}`: `{"code":3,"message":"InvalidArgument",…}` (live).
  - Source: chunk `254-…` (`v` fcn), chunk `965-…` (`hl` fcn, called with `{phone, region_code, code_key?}`).
- **No separate "verifySmsCode" in authn**; verification for login is done by submitting the SMS code to `smsLogin` (B.4).

### Family 2: idp/v1 (authenticated; `Authorization: Bearer`) — account self-service
Base `https://iam.sensecoreapi.cn/iam/idp/v1` + `users/{user_id}` (user_id from profile):
- `GET /users/{user_id}` → profile (CONFIRMED).
- `POST /users/{user_id}:sendSmsCode` body `{}` → `{token_code}` — used for change-phone step 1 (verify old phone).
- `POST /users/{user_id}:verifySmsCode` body `{"code": "<sms>", "token_code": "<token_code>"}` → ok (advances change-phone step; no new token returned).
- `POST /users/{user_id}:sendNewSmsCode` body `{"phone": "<new>", "token_code": "<old token>", "region_code": "86"}` → `{token_code}` (new-phone code).
- `POST /users/{user_id}:verifyNewSmsCode` body `{"phone": "<new>", "code": "<sms>", "token_code": "<token>", "region_code": "86"}` → ok.
- `POST /users/{user_id}:novaSendUserSmsCode` body `{}` → `{token_code}` — **SMS-to-allow-change-password**.
- `POST /users/{user_id}:novaUpdatePassword` body `{"token_code": "<token>", "password": "<new>", "verify_code": "<sms>"}` → ok.
- Sources: chunk `4582-8e90e765a90893e0.js` module 43642 (client), dialogs in same chunk: 63450 (change password), 66927 (change phone).

**Answer to "do they return a token/session?"**: the `*SmsCode*` endpoints return `{token_code}` (a short-lived flow token that must accompany the next verify/update call); the `verify*` endpoints return success only. The `token_code` is separate from the OIDC `access_token`. Family-1 `token_code` is bound to the phone+challenge; Family-2 `token_code` is bound to the authenticated user.

---

## B.3 Registration of a brand-new account — CONFIRMED (this is the critical one)

- **Method/URL**: `POST https://iam.sensecoreapi.cn/iam/authn/v1/auth/nova/register`
- **Headers**: `Content-Type: application/json` (skipBearerAuth).
- **Payload**: `{"token_code": "<from sendSmsCode>", "user_name": "<6-24 alnum>", "password": "<8-32, ≥3 classes>", "challenge": "<login_challenge>"}`
- **Response**: `{"redirect": "<console/oauth2 url>"}`. Register page then offers optional invitation code binding (`POST https://platform.sensenova.cn/lite/console/v1/invitation-codes/validate|consume`).
- **Sources**: chunk `9854-cc80a0597a525ef8.js` (register page calls `_.az` = module 20035 `b` = register, with exactly `{token_code, user_name, password, challenge}`), chunk `254-d6b62ebd00144e8a.js` module 20035 (`b` fcn). Error for `{}` body: `code 3 InvalidArgument` (live).
- Password is sent **plaintext** in the observed register call (register page has no JWE branch; only the password-login form encrypts). **[note]**

Flow: getCaptcha → (solve slider) → sendSmsCode `{phone, region_code, code_key}` → `token_code` → register `{token_code, user_name, password, challenge}` → `redirect` → OIDC exchange (B.1) if redirect is an authorize URL.

---

## B.4 Login-by-SMS for an existing phone — CONFIRMED

- **Method/URL**: `POST https://iam.sensecoreapi.cn/iam/authn/v1/auth/nova/smsLogin`
- **Headers**: `Content-Type: application/json` (skipBearerAuth).
- **Payload**: `{"token_code": "<from sendSmsCode>", "verify_code": "<SMS code>", "challenge": "<login_challenge>"}`
- **Response**: if multi-tenant → `{"tenant_list": [...]}` then a subsequent `loginNext` (`{challenge, username, user_id, sign}`); else → redirect-field object (`redirect_uri||redirect_to||redirect`) that the SPA navigates to, ending in the OIDC code exchange.
- **Sources**: chunk `965-32a855215515a831.js` (`X9` fcn; before calling: `checkChallenge(challenge).ok` must be true else challenge refresh), chunk `254-…` module 20035 (`y` fcn).

---

## B.5 Change password — CONFIRMED (console dialog)

- Step 1 (SMS to allow change): `POST https://iam.sensecoreapi.cn/iam/idp/v1/users/{user_id}:novaSendUserSmsCode` body `{}` → `{token_code}`.
- Step 2 (do the change): `POST https://iam.sensecoreapi.cn/iam/idp/v1/users/{user_id}:novaUpdatePassword` body `{"token_code": "<step1 token>", "password": "<new>", "verify_code": "<SMS code>"}` → ok.
- **JWE?** The console dialog sends `password` **plaintext** (`{token_code, password, verify_code}` observed in chunk `4582-…` dialog 63450). Unlike `nova/login`, no JWKS-encryption branch exists for `novaUpdatePassword`. **[note: treat plaintext as correct for now; risk-gated JWE on this endpoint is possible but not observed]**
- Public forgot-password alternative (authn, unauth): `POST …/iam/authn/v1/auth/nova/sendSmsCodeForRetrievePassword` `{username, phone, region_code}` → `{token_code}`; then `POST …/v1/auth/nova/retrievePassword` `{token_code, password, phone, username, verify_code, challenge}` → `{redirect}`. Sources: chunk `965-…`.

---

## B.6 API key management

### Family 1 — IAM-dir (CONFIRMED list/create; delete NOT found)
Base `https://iam.sensecoreapi.cn/iam/idp/v1`:
- List: `GET /apiKeys?page_size=<int>&page_token=<string>` → `{"api_keys":[...], "next_page_token": "...", "total_size": <int>}` (keys carry `id`, masked key, type). Source: chunk `4173-7e908bf4b2e8f791.js`.
- Create: `POST /apiKeys` body = key payload (fn `eN`, source chunk `4173-…`). **Exact payload fields NOT recovered from the bundle** — the only create dialog found (keys page) uses the lite family with `{displayname, key_type}`.
- Delete/revoke on this family: **NOT found in the fetched bundles.** Do not guess — use Family 2.

### Family 2 — lite BFF (CONFIRMED full CRUD; this is what the current console keys page uses — authoritative for the UI)
Base `https://platform.sensenova.cn/lite/console/v1`:
- List: `GET /metered/api-keys?key_type=&page_size=&page_token=` → `{api_keys:[…], total_count, next_page_token}`; `key_type ∈ {"API_KEY_TYPE_METERED", "API_KEY_TYPE_TOKEN_PLAN"}`.
- Create: `POST /metered/api-keys` body `{"displayname": "<≤64, /^[A-Za-z0-9\u4e00-\u9fa5-]+$/>", "key_type": "API_KEY_TYPE_TOKEN_PLAN"}` → returns the full `sk-…` key once.
- Revoke: `DELETE /metered/api-keys/{id}` (id = key `id` from list).
- Sources: chunk `3545-74623982914cab48.js` module 42878 (client) + keys page call sites (`b9` create / `gh` delete in same chunk / its lazy modules).
- **Which is authoritative**: the current `/console/keys` page drives Family 2 (lite) exclusively; Family 1 (IAM-dir) is a live, parallel surface (and the `/console/usage-keys` route also exists) but its delete is unknown. For a script: prefer Family 2 for list/create/revoke.

---

## B.7 Captcha / anti-bot / headers

- **Captcha (CONFIRMED, live)**: 
  - `GET https://iam.sensecoreapi.cn/iam/authn/v1/auth/getCaptcha` → `{"code_key","image"(base64 PNG),"block"(base64 PNG)}` — `block` is a slider puzzle; the login UI renders a drag slider and calls `checkCaptcha` to validate.
  - `GET /v1/auth/checkCaptcha?code_key=<key>&code_value=<answer>` → `{result…}` (validates the slider/answer).
  - `GET /v1/auth/checkChallenge?challenge=<challenge>` → `{is_valid, redirect, platform}`; the SPA calls it before smsLogin and refreshes expired challenges.
  - The `code_key` from getCaptcha is threaded into `sendSmsCode` (`code_key` field) — i.e. SMS send is gated by a solved captcha, at least sometimes.
- **Device fingerprint / other anti-bot**: **none observed** in the bundles — no fingerprint/payload-repudiation headers; just `Accept-Language` and Bearer. Risk-control (`reason:""` / 403 风控) is server-side per the API docs error table.
- Hosting note: OAuth2 is Ory Hydra (jwks `kid "public:hydra.openid.id-token"`); Hydra client id `nova`; PKCE `S256` inferred.

---

## B.8 Confidence summary / biggest unknowns

CONFIRMED (code + live where noted): all authn endpoints (register, login, loginNext, smsLogin, sendSmsCode, retrievePassword pair, captcha trio), idp/v1 user endpoints incl. novaSendUserSmsCode/novaUpdatePassword, /oauth2/token (+ /oauth2/auth existence), lite api-keys CRUD, IAM apiKeys list/create, JWKS, error envelope.
INFERRED: exact `/oauth2/auth` query parameter set (client_id=nova + PKCE params, Hydra standard), possibility that some requests require the slider captcha 100% of the time, IAM-family create-key payload, IAM-family key delete (unknown — use lite family).

**Single biggest remaining unknown for auto-registration**: reliably solving the **slider captcha** (`getCaptcha` → drag puzzle → `checkCaptcha` → `code_key` into `sendSmsCode`) and confirming the `challenge` source that must accompany register/smsLogin — i.e. the pre-registration flow (minting/obtaining a valid `login_challenge` via `oauth2/auth`) is the part not yet exercised end-to-end.

---

## Sources
- ejiema: raw HTML/JS of <https://www.ejiema.com/api.html>; live probes of api.ejiema.com (returns `ERROR:缺少参数：token`).
- SenseNova SPA chunks fetched from <https://platform.sensenova.cn/> (2026-10-09): `4039-e42da8a6395288d2.js` (auth-sdk module 56053, request helper 54511), `254-d6b62ebd00144e8a.js` (authn client module 20035), `965-32a855215515a831.js` (login page), `9854-cc80a0597a525ef8.js` (register page), `4582-8e90e765a90893e0.js` (IAM idp client module 43642 + dialogs 63450/66927), `4173-7e908bf4b2e8f791.js` (IAM apiKeys), `3545-74623982914cab48.js` + keys page (lite api-keys), `layout-ca966232fcc582bf.js` (OAuth2 exchange). Console image `nova-platform-web-console:1.1.0-20260929-40fec6d`.
- Official OpenSenseNova flow docs (register→console→API-Key 管理): <https://github.com/OpenSenseNova/SenseNova6.8/blob/main/API_CN.md>; help center <https://console.sensecore.cn/micro/help/docs/model-as-a-service/nova/>.

---

## B.9 Captcha-requirement probe (2026-10-09, read-only)

### Probe 1 — getCaptcha x3
Every call returns all three fields; `code_key` changes each call (server-side per-challenge key):
```json
{"code_key":"01a11c6e73947151a746561d7320f454","image":"<base64 PNG>","block":"<base64 PNG>"}   // x3, keys always = [block, code_key, image]
```
No response field indicates "captcha optional/required"; captcha is always *available*, not always *required*.

### Probe 2 — sendSmsCode WITHOUT code_key (phone 10000000000, region_code 86)
```json
HTTP 200
{"token_code":"01a11c6e7ccc71acaf1f67025eb5045c"}
```
No captcha demand, no phone-format rejection. **Captcha is NOT mandatory for every SMS send** on a clean context.

### Probe 3 — sendSmsCode WITH a raw (unsolved) code_key
```json
HTTP 400
{"code":3,"message":"InvalidArgument","details":[
  {"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"invalidCaptcha","domain":"iam","metadata":{}},
  {"@type":"type.googleapis.com/google.rpc.LocalizedMessage","locale":"en","message":"invalid captcha"},
  {"@type":"type.googleapis.com/sensetime.core.higgs.error_detail.v1.LogInfo","log_id":"…","track_id":"…"}]}
```
So an unsolved `code_key` is rejected: the captcha IS validated when a code_key is supplied. `checkCaptcha` expects `code_value` as an **integer** (`parsing field "code_value": strconv.ParseInt: parsing "": invalid syntax` on empty value).

### Probe 4 — SPA condition for showing the slider (chunk `965-32a855215515a831.js`)
The SMS panel handler:
```js
let B=async e=>{... let{token_code:r}=await (0,h.hl)({phone:t,region_code:p.replace(/^\+/,""),code_key:e});
  if(!r)return void A(!0);          // NO token_code => show slider captcha
  y(r),A(!1),...}
```
The slider dialog is mounted as `<er,{open:E,onOpenChange:A,onSuccess:e=>B(e)}>`; the send button calls `B()` with no args first (no code_key). **Conclusion: the UI always tries sendSmsCode captcha-free first; the captcha becomes mandatory only when the server omits `token_code` (risk-based trigger).** Chunk `254-…` module 20035 `v` = same client: `POST {iam}/iam/authn/v1/auth/nova/sendSmsCode`, `data:e`.

### Probe 5 — how the `challenge` (login_challenge) is minted (chunk `4039-…` module 56053, `RK`/`I`)
```js
async function I(e,t){...
  {codeChallenge:c,codeVerifier:u}=await (0,o.oj)();   // PKCE (S256)
  h=(0,o.nQ)(20);                                       // random state
  let d={response_type:"code",client_id:s,code_challenge_method:"S256",code_challenge:c,
         redirect_uri:a.callbackUrl,scope:"openid offline offline_access",state:h,lang:"zh-CN|en-US"};
  "register"===e&&(d.intent="register");
  localStorage.setItem("code_verifier",u);localStorage.setItem("state",h);localStorage.setItem("redirect_url",a.callbackUrl);
  window.location.href="".concat(a.signinUrl,"/oauth2/auth?").concat((0,o.pg)(d))}
```
i.e. the SPA redirects the browser to **Hydra authorize** `https://platform.sensenova.cn/oauth2/auth?response_type=code&client_id=nova&code_challenge_method=S256&code_challenge=…&redirect_uri=https://platform.sensenova.cn&scope=openid offline offline_access&state=…&lang=zh-CN` (plus `intent=register` for registration). Hydra mints the **login_challenge** and redirects to its login UI (`/login?login_challenge=…`); the login page reads it via `useSearchParams().get("login_challenge")` (chunk `965-…` `ec`/`A`). The authN calls (`login`/`register`/`smsLogin`) pass that challenge back; on success the backend returns a `redirect` toward Hydra, which redirects back to `redirect_uri` with `?code=…`, and the SPA exchanges it at `POST platform.sensenova.cn/oauth2/token` with PKCE `code_verifier`. Expired challenges → `refreshChallenge()` → repeat authorize redirect.

## B.10 Verdict — is fully-automated registration feasible?

**Yes, feasible without a human, with caveats:**
- No unconditional captcha: on a clean/new context `sendSmsCode` returns `token_code` with NO `code_key` (proved). The slider is **risk-based**: when the backend decides captcha is needed it omits `token_code`, and only then does the flow require solving the puzzle (`checkCaptcha(code_key, <int answer>)` → `code_key` → retry `sendSmsCode`). So:
  - 1st path (clean): getCaptcha not needed at all → sendSmsCode → register. Fully scriptable.
  - 2nd path (risk-triggered): must solve the numeric slider puzzle (integer `code_value` from drag position). This is solvable (CV/OCR or pixel-diff of the two base64 images), but it is the only piece that cannot be *guaranteed* headless-free.
- The `login_challenge` mint is scriptable: GET `https://platform.sensenova.cn/oauth2/auth?...&intent=register` with your own PKCE, follow the 302 to `/login?login_challenge=…`, capture it, then `register`.
- **Biggest remaining unknown**: the exact risk thresholds that flip captcha on (per IP/phone/attempt-count) — only determinable by empirical testing against your target context. Register/SMS spend no user balance (no token involved) but do cost the platform real SMS sends on success; test with a real disposable number, not "10000000000".
