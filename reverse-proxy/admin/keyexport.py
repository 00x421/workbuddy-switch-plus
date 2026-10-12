"""密钥导出：把一把网关密钥生成成 cc-switch / ZCode 的配置片段。

移植自 `ithtelab/workbuddy-manager` 的 `server/services/keyexport.py`，
但有一处**必须改**的架构差异，写在最前面：

我们没有 `cn:` / `global:` 前缀
--------------------------------
manager 的网关用**带前缀的模型 id**（`cn:glm-5.2` / `global:glm-5.2`）来表达版本归属，
并在转发给上游前把前缀剥掉。本项目的 `core/converter.py` **完全没有这层**——
`/v1/models` 返回的就是裸名（`deepseek-v4.1-flash`），数据面把 `body["model"]` 原样
转发给上游。所以这里**不能照抄前缀逻辑**：真给客户端塞一个 `cn:glm-5.2`，
它会被原样打到上游，换来一个 400。

`gateway_model_id()` 因此只做一件事：把用户误填进来的 `cn:` 前缀剥掉（那是
manager 的写法，在我们这儿没有意义），其余原样保留。

两种 base url 口径，不能混用（这条与 manager 完全一致，是客户端 SDK 的行为）

| 客户端 | 需要的值 | 原因 |
|---|---|---|
| cc-switch / claude | `http://host`（根，**不带** `/v1`） | Anthropic SDK 自己拼 `/v1/messages` |
| cc-switch / codex  | `http://host/v1` | Codex 在 base url 后拼 `/responses` |
| ZCode（openai-responses） | `http://host/v1` | 同上 |

给 claude 填 `…/v1` 是最容易犯的错：实际请求变成 `…/v1/v1/messages`，
报 **405** 而不是 404——看起来像"方法不对"，很容易把人引到错误方向去查。

与 manager 的第二个差异：**明文密钥随时可取**
------------------------------------------------
manager 只存密钥哈希，所以它的导出只能在「创建那一刻」拿到明文。本项目
`keysvc.public_key()` 有意回显并落盘明文（单机自用，见那里的说明），
因此这里可以对任意一枚已有密钥导出，入口不需要"当场传明文"这条约束。
"""
from __future__ import annotations

# 支持的客户端。cc-switch 还要再分应用（claude / codex），ZCode 只有一个。
CLIENTS = ("ccswitch", "zcode")
CCSWITCH_APPS = ("claude", "codex")

# manager 的版本前缀写法。我们只认它、并剥掉它——见模块开头的说明。
_MANAGER_PREFIXES = ("cn:", "global:")


def gateway_model_id(model: str) -> str:
    """把模型名整理成**我们网关口径**的名字。

    唯一会改动的情况：名字带了 manager 风格的 `cn:` 前缀——那是别人的路由约定，
    我们这里没有这层，留着会被原样转发给上游。`global:` 同理。
    """
    name = str(model or "").strip()
    if not name:
        raise ValueError("模型名不能为空")
    lowered = name.lower()
    for prefix in _MANAGER_PREFIXES:
        if lowered.startswith(prefix):
            return name[len(prefix):].strip()
    return name


def gateway_base_url(public_base: str) -> str:
    """面板对外地址 → OpenAI 兼容 base url（**带 `/v1`**）。

    用于 codex / ZCode 这类 `openai-responses` 客户端：它们把 `/responses`
    拼在 base url 后面，所以这里要含 `/v1`（`…/v1` + `/responses` = 我们的 `/v1/responses`）。
    """
    base = str(public_base or "").strip().rstrip("/")
    if not base:
        raise ValueError("面板对外地址不能为空")
    return base if base.endswith("/v1") else base + "/v1"


def anthropic_base_url(openai_base_url: str) -> str:
    """OpenAI 兼容 base url → **Anthropic** 口径的 base url（**不带 `/v1`**，且带尾斜杠）。

    Claude Code 走 Anthropic SDK，SDK 自己会拼 `/v1/messages`，所以
    `ANTHROPIC_BASE_URL` 必须是根地址；尾斜杠保留是因为 SDK 直接做字符串拼接，
    不补斜杠会得到 `http://hostv1/messages`。
    """
    base = str(openai_base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("面板对外地址不能为空")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return base + "/"


def _pick_models(models: list[str] | None, fallback: str | None) -> list[str]:
    """规范化模型清单；空清单时至少给一个默认模型（否则配置不可用）。"""
    out = [gateway_model_id(m) for m in (models or []) if str(m or "").strip()]
    if not out:
        if not fallback:
            raise ValueError("没有可用的模型：请为该密钥填写模型白名单后重试")
        out = [gateway_model_id(fallback)]
    # 去重但保留顺序（客户端按这个顺序展示）
    seen: set[str] = set()
    return [m for m in out if not (m in seen or seen.add(m))]


def first_model(models: list[str] | None, default_model: str | None = None) -> str:
    """这份配置实际会用的主模型。"""
    return _pick_models(models, default_model)[0]


def to_ccswitch(*, token: str, base_url: str, app: str, name: str,
                models: list[str] | None = None,
                default_model: str | None = None) -> dict:
    """生成 cc-switch 的 `providers.settings_config`（字典，调用方再 json.dumps）。

    结构取自 cc-switch 库中真实数据（manager 侧 2026-09-24 实测）：
      · claude：`{"env": {"ANTHROPIC_AUTH_TOKEN": ..., "ANTHROPIC_BASE_URL": ...}}`
      · codex ：`{"auth": {"OPENAI_API_KEY": ...}, "config": "<TOML 文本>"}`
    """
    if app not in CCSWITCH_APPS:
        raise ValueError(f"app 必须是 {CCSWITCH_APPS} 之一，收到 {app!r}")
    if not str(token or "").strip():
        raise ValueError("密钥不能为空")
    if not str(name or "").strip():
        raise ValueError("供应商名不能为空")

    picked = _pick_models(models, default_model)
    chosen = picked[0]

    if app == "claude":
        return {"env": {
            "ANTHROPIC_AUTH_TOKEN": token,
            # 根地址而非 /v1：SDK 自己拼 /v1/messages（见 anthropic_base_url）
            "ANTHROPIC_BASE_URL": anthropic_base_url(base_url),
            "ANTHROPIC_MODEL": chosen,
            # 三个档位都指向同一模型：本网关按模型名路由，没有 haiku/sonnet/opus
            # 的概念；留空会让 cc-switch 回退到官方模型名而打到错误的端点。
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": chosen,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": chosen,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": chosen,
        }}

    # codex：TOML 文本。转义反斜杠与双引号，以免模型名里出现引号时破坏 TOML。
    def _q(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [
        'model_provider = "workbuddy"',
        f"model = {_q(chosen)}",
        "",
        "[model_providers]",
        "[model_providers.workbuddy]",
        f"name = {_q(name)}",
        # 我们对外提供 /v1/responses（OpenAI Responses 兼容形态）
        'wire_api = "responses"',
        "requires_openai_auth = true",
        f"base_url = {_q(gateway_base_url(base_url))}",
        "",
    ]
    return {"auth": {"OPENAI_API_KEY": token}, "config": "\n".join(lines)}


def to_zcode(*, token: str, base_url: str, name: str, provider_id: str,
             models: list[str] | None = None,
             default_model: str | None = None,
             context_windows: dict[str, int] | None = None) -> dict:
    """生成 ZCode 的供应商配置**片段**（调用方按 `providerId` 并入 provider_config.json）。

    返回片段而非整份文件：整份文件里还有用户自己的其它供应商与顺序，
    导出端不该替用户决定 `providerOrder` 的全量内容。
    """
    if not str(provider_id or "").strip():
        raise ValueError("providerId 不能为空")
    if not str(token or "").strip():
        raise ValueError("密钥不能为空")
    if not str(name or "").strip():
        raise ValueError("供应商名不能为空")

    picked = _pick_models(models, default_model)
    ctx = context_windows or {}
    rules = []
    for model_id in picked:
        props: dict[str, int] = {}
        window = ctx.get(model_id)
        if window:      # 拿不到上下文长度就不写，让客户端用自己的默认值
            props["contextWindow"] = int(window)
        rules.append({"modelId": model_id, "providerId": provider_id,
                      "config": {"properties": props}})

    return {
        "providerRule": {
            "providerId": provider_id,
            "providerName": name,
            "config": {
                "group": "standard-personal",
                "access": {"type": "api-key", "apiKey": token},
                "api": {"type": "openai-responses", "baseUrl": gateway_base_url(base_url)},
                "personalModelIds": list(picked),
                "modelOrder": list(picked),
            },
        },
        "providerModelRules": rules,
    }


def build(*, client: str, key: dict, name: str, base_url: str,
          models: list[str] | None = None, default_model: str | None = None,
          app: str | None = None, provider_id: str | None = None,
          context_windows: dict[str, int] | None = None,
          known_models: list[str] | None = None) -> dict:
    """统一入口：按客户端产出配置片段，并附上给人看的接入说明。

    `key` 是 `keysvc.public_key()` 的产物（含明文 `secret`）。
    `models` 为空时依次退回：密钥白名单 → `default_model`。
    """
    if client not in CLIENTS:
        raise ValueError(f"client 必须是 {CLIENTS} 之一，收到 {client!r}")
    token = str(key.get("secret") or "").strip()
    if not token:
        # 老条目没有落明文（早期版本只存哈希）——这时只能让用户重建一枚
        raise ValueError("这枚密钥没有可导出的明文（早期版本只存了哈希），请新建一枚后再导出")

    picked = list(models or key.get("models") or [])
    if not picked:
        picked = [default_model] if default_model else []
    if known_models:        # 只导出本机确实可用的模型，避免配置里塞进用不了的 id
        allowed = set(known_models)
        picked = [m for m in picked if gateway_model_id(m) in allowed] or picked

    label = name or key.get("name") or "WorkBuddy"
    kw = dict(token=token, base_url=base_url, name=label, models=picked,
              default_model=default_model)

    if client == "ccswitch":
        app = app or "claude"
        payload = to_ccswitch(app=app, **kw)
        return {
            "client": client, "app": app, "name": label,
            "endpoint": gateway_base_url(base_url) if app == "codex" else anthropic_base_url(base_url),
            "format": "toml" if app == "codex" else "json",
            "settings_config": payload,
            "models": _pick_models(picked, default_model),
            "provider_id": provider_id or "",
        }

    pid = provider_id or "workbuddy"
    payload = to_zcode(token=token, base_url=base_url, name=label,
                       provider_id=pid, models=picked, default_model=default_model,
                       context_windows=context_windows)
    return {
        "client": client, "app": "openai-responses", "name": label,
        "endpoint": gateway_base_url(base_url),
        "format": "json",
        "fragment": payload,
        "models": _pick_models(picked, default_model),
        "provider_id": pid,
    }
