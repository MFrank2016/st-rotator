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
6. **打标签并推送**：给本次发版的 commit 打一个 **annotated tag**（命名 `v<版本号>`，与 `version.py` 严格一致），并推送：

   ```bash
   git tag -a v1.3.7 -m "v1.3.7"
   git push origin v1.3.7
   ```

   > 用 annotated tag（`-a`）而非轻量 tag；`-m` 可放一句话发版摘要。tag 名必须与
   > `version.py` 的版本号一致（如 `v1.3.7`），一个版本号只打一个 tag。

7. **服务器拉取并重新部署**：

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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **st-rotator** (3155 symbols, 8095 relationships, 279 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/st-rotator/context` | Codebase overview, check index freshness |
| `gitnexus://repo/st-rotator/clusters` | All functional areas |
| `gitnexus://repo/st-rotator/processes` | All execution flows |
| `gitnexus://repo/st-rotator/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
