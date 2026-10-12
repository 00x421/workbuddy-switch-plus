# 反代服务（workbuddy-switch-plus / reverse-proxy）

把 **WorkBuddy / CodeBuddy 的登录态**转成标准 **OpenAI / Anthropic 兼容 API** 的本地服务。
基底是 [`ShouZhuo0413/codebuddy2api`](https://github.com/ShouZhuo0413/codebuddy2api)（Python），
账号来源改为**直读并热加载 WB-Switch-Plus 的账号库**，并从
[`ithtelab/workbuddy-manager`](https://github.com/ithtelab/workbuddy-manager) 移植了
**多密钥分发 + IP/模型白名单 + 额度**与**多用户鉴权**两套安全能力。
可打包成**单文件 exe**（`python build-exe.py`），双击即用、无需 Python 环境。

## 一、它做什么

- 读取账号凭据，注入腾讯鉴权头（`Authorization: Bearer` + `X-User-Id` / `X-Enterprise-Id` / `X-Domain`）
- 转发到腾讯后端 `https://copilot.tencent.com/v2/chat/completions`
- 对外暴露 OpenAI / Anthropic 双协议：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | OpenAI Chat（流式 / 非流式，原生 tools） |
| POST | `/v1/responses` | OpenAI Responses（适配 Codex CLI） |
| POST | `/v1/messages` | Anthropic Messages（适配 Claude Code / cc-switch） |
| POST | `/v1/messages/count_tokens` | 输入 token 粗估 |
| GET | `/v1/models` | 模型列表 |
| GET | `/health` | 健康检查 |

内置**账号池**（`admin/pool.py`）：轮询 / 手动路由、会话亲和、401/403→冷却 5 分钟、
402/429→冷却 30 分钟、积分耗尽态、自动签到、token 过期自动刷新。

管理端分两套入口，同一份 API：

| 入口 | 地址 | 鉴权 |
| --- | --- | --- |
| 内置静态后台 | `http://127.0.0.1:8787/admin/` | Cookie + CSRF（同源） |
| switch SPA | 桌面端侧边栏「反代服务」`/reverse-proxy` | `X-Admin-Token` 头 + CORS |

## 二、相对基底的改造（本次适配）

### 2.1 账号来源与注入

| 位置 | 改造 |
| --- | --- |
| `admin/switch_store.py`（新增） | `SwitchStore` + `SwitchCredentialManager`：账号来源改为 **`~/.wb-switch-plus/accounts.json`**，实现 `Store` 的全部接口，池化/协议/鉴权逻辑零改动复用 |
| `admin/server.py` | `create_app(..., store=None)`：允许注入 store（默认仍为原 `.info` 文件模式，向后兼容） |
| `serve.py`（新增） | 独立服务入口：用 `SwitchStore` 起服务，默认 `127.0.0.1:8787` |
| `core/` 等 | **未改动**，原有 102 个测试全部通过 |

### 账号字段映射（switch → 服务）

| switch 账号库字段 | 服务需要的形状 |
| --- | --- |
| `access_token` / `refresh_token`（明文或 `$wbEncrypted` 信封） | `auth.accessToken` / `auth.refreshToken`（原样透传，用时解密） |
| `expiresAt` / `refreshExpiresAt`（毫秒） | `auth.expiresAt` / `auth.refreshExpiresAt` |
| `domain`（缺省 `www.codebuddy.cn`） | `auth.domain` |
| `uid` / `nickname` / `enterpriseId` / `profile_raw` | `account.uid` / `.nickname` / `.enterpriseId` |
| `variant`（`cn` / `ai`） | `realm`（`cn` / `global`） |

**只读**：服务不写账号库（账号增删由 WB-Switch-Plus 负责）。刷新后的 token 默认只留内存；
设 `WB_SWITCH_WRITE_BACK=1` 才原子回写。

### 2.2 从 workbuddy-manager 移植的安全能力

manager 用 SQLite + FastAPI；这里**不引入数据库**，全部落到 `state.json`（`store.data`）。

| manager 模块 | 本项目落点 | 存储键 |
| --- | --- | --- |
| `server/security.py`（PBKDF2 会话 / 防爆破 / 作用域令牌 / 审计） | `admin/security.py`（新增） | `secret` `users` `api_tokens` `audit_logs` `security` |
| `server/routers/keys.py` + `keysvc.py`（多密钥 + IP/模型白名单 + 额度） | `admin/keysvc.py`（新增，路由并入 `admin/server.py`） | `keys` `api_key_ips` |
| `server/iputil.py`（可信代理下的真实客户端 IP + CIDR 判定） | `admin/iputil.py`（新增） | — |
| `server/routers/security.py`（IP 白/黑名单） | `admin/server.py` 的 `IPGuardMiddleware` | `ip_rules` `ip_access_logs` |

保留的安全不变量：

- 口令只存 `pbkdf2_sha256$260000$salt$hash`；会话与令牌只存哈希
- 防爆破按 **IP + 用户名**双维度计数，`MAX_FAILS=8` / `LOCK_SECONDS=600`，锁定条目永不被清理覆盖
- 管理令牌作用域 `readonly` / `admin`；令牌自管理接口只收**会话管理员**
- 改用户名/口令/角色即撤销该用户全部会话
- 撤销密钥有「至少保留一枚」约束（与基底一致），避免把数据面锁死

密钥拒绝语义（与 manager 对齐）：

| 情形 | 状态码 | `error.code` |
| --- | --- | --- |
| 密钥禁用 / 过期 | 403 | — |
| 额度（token 或积分）用尽 | 429 | `insufficient_quota` |
| 档位（realm）/ 模型白名单 / 缺 model | 400 | `invalid_request_error` |
| 来源 IP 不在白名单 | 400 | `ip_not_allowed` |
| 来源 IP 数超过 `max_ips` | 400 | `too_many_ips` |

### 2.3 账号库热加载

WB-Switch-Plus 里增删 / 重新登录账号后，服务**不需要重启**即可跟上：

| 机制 | 说明 |
| --- | --- |
| 指纹短路 | 每次 `account_rows()` 前比一次 `(mtime_ns, size)`；没变就直接返回，代价只有一次 `stat()` |
| 后台轮询 | `lifespan` 里的 `accounts_worker` 默认每 2 秒比对一次（`WB_ACCOUNTS_POLL` 可调），保证数据面调度也跟上 |
| 变更摘要 | `refresh_accounts()` 返回 `{changed, added, removed, renamed, total, active}`，并在控制台打一行 `[accounts] 账号库已重载（+1）` |
| 僵尸清理 | 账号消失时，它的 `account_status`、`session_bindings`、凭据管理器缓存一并回收；`active` 账号消失则自动改选 |
| 瞬时失败保护 | 账号库被占用 / 读到半截内容时返回 `stale=True` 并**保留现状**，绝不把账号清空 |
| 本地覆盖层 | 暂停 / 改名 / 从本服务移除存在 `state.json` 的 `account_overrides`，不会被账号库刷新抹掉 |

「谁说了算」的边界：

| 维度 | 权威来源 |
| --- | --- |
| 账号的存在性、凭据、档位（realm） | WB-Switch-Plus 账号库（服务只读） |
| 启用 / 暂停 / 改名 | 本服务（`account_overrides`） |
| 从本服务移除 | 本服务（`account_overrides[aid].hidden=true`）——账号库里仍保留 |

因此管理界面里的「移除」= 不再参与本服务的调度与签到，接口返回 `{"ok": true, "scope": "service"}`；
要彻底删账号请到 WB-Switch-Plus 的账号管理页。移除只针对已知 id，所以即使账号库此刻读不到也会立刻生效。

### 2.4 SPA 侧改造

| 位置 | 内容 |
| --- | --- |
| `src/lib/reverse-proxy.ts`（新增） | 跨源管理客户端：base URL / 令牌存 localStorage，请求带 `X-Admin-Token`、`credentials: "omit"` |
| `src/components/reverse-proxy/*.tsx`（新增 10 个） | 概览 / 账号池 / API 密钥 / 管理令牌 / 用户 / IP 安全 / 连接测试 / 接入指南 / 确认弹窗 / 共用原子 |
| `src/pages/ReverseProxyPage.tsx`（新增） | 页面外壳：登录门 → 8 个标签页，概览 30s 轮询，角色徽标与退出 |
| `src/App.tsx` | 侧边栏新增「反代服务」入口与 `/reverse-proxy` 路由 |

跨源方案：服务端在 `/admin*` 上原本发 `X-Frame-Options: DENY` + `frame-ancestors 'none'`，
**无法 iframe 内嵌**，因此改为原生 React 移植；同时服务端开放 CORS 并支持头令牌，
内置静态后台的 Cookie + CSRF 通道保持不变。

## 三、运行

### 3.1 源码运行

```bash
pip install -r requirements.txt        # fastapi / uvicorn / httpx / cryptography
python serve.py                        # 默认 127.0.0.1:8787
python serve.py --port 9000 --open     # 指定端口并打开内置后台
```

命令行参数：`--host` / `--port` / `--data` / `--accounts` / `--open` / `--no-banner`。

### 3.2 打包成单文件 exe

```bash
pip install pyinstaller
python build-exe.py                    # 产出 dist/wb-reverse-proxy.exe（约 21 MB）
python build-exe.py --onedir           # 目录形态（启动更快）
```

打包后的行为：

- **双击即启动**，控制台打印管理地址、账号库路径，以及首次启动生成的管理员口令；
- 单独运行（双击 exe）时，运行数据落在 **exe 同级的 `data/`**（绿色免安装，拷走整个文件夹就能带走配置）；
  该目录不可写时自动退回 `%APPDATA%\workbuddy-switch-plus`；
- **由桌面端托管时**，桌面端会显式传 `--data`，把运行数据归到
  `~/.wb-switch-plus/reverse-proxy/`（二改版所有落盘数据统一在这个目录名下），
  首次归位会把 exe 同级 `data/state.json` 整份搬过来，不丢管理口令与签到记录；
- 内置静态后台（`admin/static`）随包带走，`http://127.0.0.1:8787/admin/` 直接可用；
- 需要 Python 侧依赖（`fastapi` / `uvicorn` / `httpx` / `cryptography`）参与打包，
  但**运行机器无需 Python 环境**。

> 首次启动生成的管理员口令只打印一次；之后启动沿用同一口令。
> 想自定义：设 `WB_ADMIN_PASSWORD` 并删掉 `data/state.json` 再启动。

### 3.3 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WB_SWITCH_ACCOUNTS` | `~/.wb-switch-plus/accounts.json` | switch 账号库路径 |
| `WB_SWITCH_DATA` | 源码 `./data`／exe 同级 `data`／被桌面端托管时 `~/.wb-switch-plus/reverse-proxy` | 本服务 state（密钥 / 用户 / 令牌 / IP 规则 / 池设置）；命令行 `--data` 优先 |
| `WB_ACCOUNTS_POLL` | `2` | 账号库热加载轮询间隔（秒，最小 0.5） |
| `WB_SERVE_HOST` / `WB_SERVE_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `WB_SERVE_OPEN` | `0` | 启动后打开内置管理后台 |
| `ADMIN_KEY` | 随机生成并打印 | 首次启动用它当 `admin` 的初始口令（≥20 字符） |
| `WB_ADMIN_PASSWORD` | 同 `ADMIN_KEY` | 显式指定 `admin` 初始口令 |
| `WB_ALLOW_ADMIN_KEY` | `1` | 是否保留旧的「单密钥登录」通道 |
| `WB_ADMIN_CORS_ORIGINS` | `*` | 管理 API 允许的跨源来源，逗号分隔 |
| `WB_TRUST_PROXY` | `0` | 是否信任 `X-Forwarded-For`（前面挂了反代才开） |
| `WB_TRUSTED_PROXY_CIDRS` | 内网段 | 可信代理来源段 |
| `CODEBUDDY2OPENAI_KEY` | 空 | 预置的下游 API Key |
| `WB_SWITCH_WRITE_BACK` | `0` | 是否把刷新结果回写账号库 |


## 四、已验证

**Python 侧**（Windows / Python 3.13，`python -m pytest`）：**125 passed**

- 基底原有 102 个
- 安全能力 +12：多用户登录与角色、只读令牌拒写、按用户名的防爆破锁定、用户 CRUD 保底一个管理员、
  改密撤销会话、作用域令牌（明文不落盘、只读不能改、令牌接口仅会话）、密钥 IP/模型白名单在
  `/v1/chat/completions` 上生效、CIDR 写入校验、`keysvc` 的额度/档位/模型规则、重置用量、
  IP 黑名单拦截、审计日志、check-models
- 热加载 +11：新增账号即时可见、删除账号并回收其 `account_status`/`session_bindings`、
  `active` 账号消失后自动改选且数据面凭据同步、暂停/改名扛得住刷新、服务侧移除不写账号库且不复活、
  账号库读不到时保持现状（`stale`）、账号库缺失视为空、指纹不变不重载、移除在账号库不可读时也立刻生效

**源码方式冒烟**（`SwitchStore` + `serve.py`，账号取自 `~/.wb-switch-plus/accounts.json`）：

- 3 个账号、35 个模型正常载入；`/health` 200；启动横幅正确
- 跨源预检 `OPTIONS` 返回 `access-control-allow-origin` 与 `X-Admin-Token`
- 头令牌登录 → `/session` 返回 `{username, role: "admin", via: "session"}`；`/overview` 密钥列表不含 `hash`
- 头令牌写通道：创建/编辑/重置密钥、签发与撤销管理令牌、增删用户、增删 IP 规则、`check-models`
  （真实模型名不误报、拼错名报出）全部通过
- 审计动作落盘：`login` / `create_key` / `update_key` / `reset_key_usage` / `create_token` /
  `add_user` / `add_ip_rule` / `delete_ip_rule` / `delete_token`

**exe 冒烟**（`dist/wb-reverse-proxy.exe`，21.2 MB，账号库换成合成样本）：

```
health            : ok
内置后台 index     : 200
管理登录          : ok
初始账号          : ['冒烟A', '冒烟B']
账号库新增后       : ['冒烟A', '冒烟B', '冒烟C'] （服务未重启）
账号库删除后       : ['冒烟B', '冒烟C'] （服务未重启）
账号库读不到时     : ['冒烟B', '冒烟C'] （应保持不变）
服务侧移除         : {'ok': True, 'scope': 'service'} → ['冒烟B']
账号库恢复后       : ['冒烟B'] （被移除的不会复活）
账号库仍保留       : ['smoke-b', 'smoke-c']
/v1 无密钥        : 401
```

- 运行数据落在 exe 同级 `data/`（`C:\...\wbrp-exe\data`），符合绿色免安装预期
- 控制台横幅中文正常（已把控制台输出代码页切到 UTF-8）

**SPA 侧**：`tsc --noEmit` 无错、`vite build` 成功、`vitest run` 44 passed。
（运行时截图未做：本机未装 agent-browser，改用等价的跨源 HTTP 链路验证。）

## 五、待办

1. 与 Tauri 桌面端的联动：桌面端一键拉起/关闭这个 exe（当前是各自独立启动）
2. 可选：密钥导出/导入（cc-switch / ZCode）、审计日志清理入口、账号池签到日志清理
3. 可选：exe 加版本资源与数字签名
