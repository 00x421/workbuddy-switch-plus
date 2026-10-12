"""产品配置的来源优先级：动态缓存 > 装机快照。

背景（本机实测，2026-10）：

| 来源 | 文件 | 大小 | 模型数 | 有 deepseek-v4.1-flash |
| --- | --- | --- | --- | --- |
| 装机快照 | `<装目录>/resources/app.asar.unpacked/cli/product.json` | 378 KB | 49 | 否 |
| 动态缓存 | `~/.workbuddy/cache/acc-product-config-v3.json` | 389 KB | 55 | 是 |

两者都是 WorkBuddy 自己的文件，区别在于**快照在安装那一刻就固定了**，而缓存是客户端
运行时从服务端拉下来覆盖的。早期只读快照，于是「WorkBuddy 里选得到 deepseek-v4.1-flash、
反代却报未知模型 / 后台表单选不中」——本文件就是把这个回归钉住。

缓存路径不能写死版本号：客户端将来可能下发 v4、v5，所以按 `acc-product-config-v<N>.json`
取版本号最大的那个。
"""

import json
from pathlib import Path

import pytest

from core import converter


def _write_config(path: Path, model_ids, extra_models=()):
    """写一份最小可用的产品配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    models = [{"id": mid, "vendor": "f"} for mid in model_ids]
    models.extend(extra_models)
    path.write_text(json.dumps({"models": models}, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """把「WorkBuddy 数据目录」指到临时目录，并摘掉会抢优先级的环境变量。"""
    monkeypatch.delenv("ACC_PRODUCT_CONFIG_PATH", raising=False)
    monkeypatch.setenv("WORKBUDDY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(converter, "_default_model_cache", None)
    return tmp_path


def test_cached_config_picks_highest_version(isolated_data_dir):
    """v10 > v3 > v1：将来客户端下发 v4/v5 也要能自动跟上。"""
    for version in (1, 3, 10):
        _write_config(
            isolated_data_dir / "cache" / f"acc-product-config-v{version}.json",
            [f"model-v{version}"],
        )

    found = converter._find_cached_product_config()
    assert found is not None
    assert found.name == "acc-product-config-v10.json"


def test_cached_config_ignores_conversation_spill(isolated_data_dir):
    """`cache/conversation-product-spill/` 是按会话拆出来的副本，同一份配置会散成几百份。

    只认 `cache/` 这一层，否则会随机命中一个会话级的副本。
    """
    _write_config(isolated_data_dir / "cache" / "acc-product-config-v3.json", ["real"])
    _write_config(
        isolated_data_dir / "cache" / "conversation-product-spill"
        / "acc-product-config-v9-deadbeef.json",
        ["spilled"],
    )

    found = converter._find_cached_product_config()
    assert found is not None
    assert found.parent.name == "cache"
    assert "spill" not in str(found)


def test_env_override_wins_over_cache(isolated_data_dir, tmp_path, monkeypatch):
    """`ACC_PRODUCT_CONFIG_PATH` 是客户端自己认的覆盖项，用户显式指定就听他的。"""
    _write_config(isolated_data_dir / "cache" / "acc-product-config-v3.json", ["from-cache"])
    override = _write_config(tmp_path / "explicit.json", ["from-env"])
    monkeypatch.setenv("ACC_PRODUCT_CONFIG_PATH", str(override))

    assert converter._find_workbuddy_product_json() == override


def test_cache_wins_over_install_snapshot(isolated_data_dir):
    """核心回归：缓存必须排在装机快照前面，否则新模型永远看不见。"""
    cached = _write_config(
        isolated_data_dir / "cache" / "acc-product-config-v3.json", ["new-model"]
    )

    found = converter._find_workbuddy_product_json()
    assert found == cached, f"选了 {found}，动态缓存被装机快照盖住了"


def test_missing_cache_returns_none(isolated_data_dir):
    """没有缓存目录时不能抛异常，交给调用方继续往装机快照找。"""
    assert converter._find_cached_product_config() is None


def test_custom_local_models_are_filtered_out(isolated_data_dir, monkeypatch):
    """`custom-local:` 是用户手填的本地别名（通常 url 指回反代自己），上游不认，不能列出去。"""
    config = _write_config(
        isolated_data_dir / "cache" / "acc-product-config-v3.json",
        ["deepseek-v4.1-flash", "glm-5.2"],
        extra_models=[
            {
                "id": "custom-local:Deepseek-V4.1-Flash",
                "name": "Deepseek-V4.1-Flash",
                "vendor": "Custom",
                "url": "http://127.0.0.1:8787/v1",
            },
            {"id": "another-custom", "vendor": "Custom"},
            {"id": "internal-completion", "vendor": "tencent"},
        ],
    )
    monkeypatch.setattr(converter, "_find_workbuddy_product_json", lambda: config)

    models = converter.get_available_models()
    assert "deepseek-v4.1-flash" in models
    assert "glm-5.2" in models
    assert not [m for m in models if m.startswith(converter.CUSTOM_LOCAL_MODEL_PREFIX)]
    assert "another-custom" not in models
    assert "internal-completion" not in models


def test_available_models_come_from_cached_config(isolated_data_dir):
    """端到端：只要缓存里有，`get_available_models()` 就得看得见。"""
    _write_config(
        isolated_data_dir / "cache" / "acc-product-config-v3.json",
        ["deepseek-v4.1-flash", "deepseek-v4-flash"],
    )

    models = converter.get_available_models()
    assert "deepseek-v4.1-flash" in models
    # 解析默认模型时同样要能看见它，才能选出 deepseek-v4.1-flash 而不是退回 auto
    converter._default_model_cache = None
    assert converter.resolve_default_model() == "deepseek-v4.1-flash"


@pytest.mark.skipif(
    not (Path.home() / ".workbuddy" / "cache").is_dir(),
    reason="本机没有 WorkBuddy 数据目录",
)
def test_real_machine_regression(monkeypatch):
    """真机回归：装上 WorkBuddy 的机器上，清单必须包含 deepseek-v4.1-flash。

    只要客户端能选到这个模型，反代就该看得见它。
    """
    monkeypatch.delenv("ACC_PRODUCT_CONFIG_PATH", raising=False)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)

    source = converter._find_workbuddy_product_json()
    assert source is not None
    assert "cache" in str(source), f"没优先用动态缓存，选中的是 {source}"

    assert "deepseek-v4.1-flash" in converter.get_available_models()
