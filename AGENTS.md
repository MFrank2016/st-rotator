# AGENTS.md

给在此仓库工作的 AI / 协作者的约定。**核心是发版标准流程**，每次改动都要走一遍。

## 版本号

- 唯一来源：`st_rotator/version.py` 的 `__version__`。当前基线 **1.3.0**。
- 递增规则：
  - **小版本 `+0.0.1`** —— 修 bug、样式 / 文案、重构、文档、依赖等：`1.3.0 → 1.3.1`
  - **大版本 `+0.1`** —— 新增功能，或行为 / 接口发生变化：`1.3.0 → 1.4.0`
- **每次改动都要递增版本号**，并在 `CHANGELOG.md` 顶部新增一条对应条目。

## 发版标准流程

1. **改代码**：完成本次改动。
2. **改版本号**：按上面的规则改 `st_rotator/version.py`。
3. **写发版日志**：在 `CHANGELOG.md` 顶部加一条 —— 版本号、日期、变更内容。
4. **本地端到端验证**（必须全绿才继续）：
   - 测试：`python3 -m unittest discover -s tests -t .`
   - 起本地服务实跑：`python3 -m st_rotator ui -c config.json --port 8899 --no-open`，
     浏览器打开 `http://127.0.0.1:8899/admin`，确认关键功能正常、窄屏（移动端）样式正常。
5. **提交并推送**：
   - `git add` 只加本次相关文件，不带无关改动。
   - 提交信息用 Conventional Commits 中文风格：`feat:` / `fix:` / `style:` / `docs:` / `refactor:` / `chore:`。
   - `git push origin main`。
6. **服务器拉取并重新部署**：

   ```bash
   ssh root@107.151.246.25
   cd /root/programming/github/st-rotator
   git pull --ff-only origin main
   docker compose build
   docker compose up -d
   docker ps --format '{{.Names}} {{.Status}}' | grep st-rotator   # 应为 healthy
   ```

   容器内自检：

   ```bash
   docker exec st-rotator python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read().decode())"
   ```

> ⚠️ 服务器上 `docker-compose.yml` 有一处**本地未提交改动**（`security_opt: no-new-privileges:true`），
> 它不属于仓库版本；`git pull` 与重建都不要覆盖它（本流程用的是 `--ff-only`，不会动它）。

## 部署形态速查

- 服务器：`107.151.246.25`（root），代码在 `/root/programming/github/st-rotator`。
- 运行方式：Docker Compose 项目 `st-rotator`，镜像由仓库 `Dockerfile` 构建；容器 `st-rotator`
  监听 `8080`，宿主机发布到 `127.0.0.1:18090`，再由 Caddy 反代到公网域名。
- 数据：`./data` 挂载到容器 `/data`（含 `config.json`、`rotator.log`、`guard.json` 等），
  **含敏感信息、不要提交，也不要在部署时覆盖**。

## 代码约定

- 核心零第三方依赖；只有「账号余量 / 补号登录」用到可选的 `jwcrypto`。
- 前端是单文件内联 HTML（`st_rotator/dashboard.py`），零 CDN 依赖，由网关自己吐出。
- 提交前跑测试；`config.json` / `replenish.json` / `data/` 含敏感信息，已在 `.gitignore`。
