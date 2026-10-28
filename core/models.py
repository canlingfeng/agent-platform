"""模型注册表（Model Registry）：按【能力】解析出可直连的调用参数。

设计动机
--------
原来有两套并存的解析逻辑：`core/agent.py` 读 config.json 拿对话模型，
Embedding 脚本自己又写了一套 `pick()`。同一件事两处实现，改一处忘一处，
而且每处都硬编码了模型名（如 "Qwen/Qwen3-Embedding-0.6B"），换模型要改代码。

本模块把它收敛成唯一入口：调用方只说"我要什么能力"（chat / embedding / rerank），
具体是哪家的哪个模型、用什么密钥，全部由配置决定。

三层优先级（precedence chain）
------------------------------
1. 环境变量       —— 容器/服务器部署时改模型不用改代码（12-Factor App 第三条）
2. config.json    —— 本地开发的持久化配置
3. CAPABILITY_DEFAULTS —— 兜底，保证配置缺失时仍能启动

对应环境变量命名：`<CAPABILITY>_MODEL` / `<CAPABILITY>_BASE_URL` / `<CAPABILITY>_API_KEY`
例如 EMBEDDING_MODEL、CHAT_MODEL、RERANK_MODEL。

密钥策略
--------
config.json 只存 provider / model / base_url，**不存明文密钥**；
密钥一律从环境变量读。例外：provider="custom" 时自定义端点没有对应环境变量，
允许从 config.json 读（与 core/agent.py 的既有行为保持一致）。
"""
import json
import os
from dataclasses import dataclass

from core.providers import (
    CAPABILITY_DEFAULTS,
    PROVIDER_BASE_URLS,
    PRESETS,
)

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
)


@dataclass(frozen=True)
class ModelSpec:
    """一次模型调用所需的全部信息。

    frozen=True：创建后不可改。配置对象就该不可变，否则某个调用方偷偷改了
    spec.model，后续调用全乱套且极难排查。
    """
    capability: str      # chat / embedding / rerank
    provider: str        # 厂商 key，如 modelscope / siliconflow / custom
    base_url: str        # OpenAI 兼容地址，末尾不带斜杠
    model: str           # 具体模型 id
    api_key: str         # 缺失时为空串，由调用方决定如何报错


def _load_config(path: str = CONFIG_PATH) -> dict:
    """读 config.json。文件不存在 / 内容损坏都返回空 dict，绝不抛异常。

    容器里没有这个文件（.dockerignore 已排除），此时全部走环境变量与默认值。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _pick(env_key: str, cfg_value, default):
    """按 环境变量 > config.json > 默认值 取值。

    空字符串与 None 都视为"没配"，继续往下一层找。
    """
    value = os.environ.get(env_key) or cfg_value or default
    return value.strip() if isinstance(value, str) else value


def resolve(capability: str, cfg: dict = None) -> ModelSpec:
    """把能力名解析成 ModelSpec。

    Args:
        capability: "chat" / "embedding" / "rerank"。未知值按 chat 处理
            （防御性：别让拼错的能力名把请求打到空地址上）。
        cfg: 已加载的 config.json 内容；不传则内部自动读一次。

    Returns:
        ModelSpec。api_key 可能为空串——调用方需自行判断并给出友好报错。
    """
    if capability not in CAPABILITY_DEFAULTS:
        capability = "chat"
    if cfg is None:
        cfg = _load_config()

    defaults = CAPABILITY_DEFAULTS[capability]
    # config.json 里该能力的专属配置（新增的 models 段）
    section = cfg.get("models", {}).get(capability, {}) or {}

    # 对话能力还要兼容旧格式：config.json 顶层的 provider / base_url / model。
    # 旧版 config 只有 api_key + provider 两个字段，必须仍能正确加载。
    top = cfg if capability == "chat" else {}

    provider = _pick(f"{capability.upper()}_PROVIDER",
                     section.get("provider") or top.get("provider"),
                     defaults["provider"])
    # 防御性：未知厂商会让 base_url 变空、请求打到空地址。沿用
    # core.providers.resolve() 的既有约定——未知值回落到该能力的默认厂商。
    if provider not in PROVIDER_BASE_URLS and provider != "custom":
        provider = defaults["provider"]

    model = _pick(f"{capability.upper()}_MODEL",
                  section.get("model") or top.get("model"),
                  # 已知厂商用它自己的默认模型；没有再用能力默认值兜底
                  PRESETS.get(provider, {}).get("model") or defaults["model"])

    base_url = _pick(f"{capability.upper()}_BASE_URL",
                     section.get("base_url") or top.get("base_url"),
                     PROVIDER_BASE_URLS.get(provider, ""))

    # 密钥优先级：<CAP>_API_KEY 环境变量（provider 无关）> 默认厂商的环境变量
    api_key = (_pick(f"{capability.upper()}_API_KEY", None, "")
               or _pick(defaults["api_key_env"], None, ""))
    if not api_key and provider == "custom":
        # 自定义端点若未设 <CAP>_API_KEY，才回落到 config.json（明文，仅限本地开发；
        # 容器部署没有 config.json，届时务必用环境变量）
        api_key = (section.get("api_key") or cfg.get("api_key") or "").strip()

    return ModelSpec(
        capability=capability,
        provider=provider,
        base_url=base_url.rstrip("/") if isinstance(base_url, str) else "",
        model=model,
        api_key=api_key,
    )


def is_configured(capability: str, cfg: dict = None) -> bool:
    """该能力是否已配好密钥，可直接调用。供入口做前置校验。"""
    return bool(resolve(capability, cfg).api_key)


def known_providers(capability: str = "chat") -> list:
    """返回支持某能力的厂商 key 列表，供 UI / 校验使用。

    对话能力只返回 PRESETS 里的厂商（与前端模型选择器保持一致）；
    其他能力返回 PROVIDER_BASE_URLS 里已知 base_url 的厂商。
    """
    if capability == "chat":
        return [k for k in PRESETS if k != "custom"]
    return [k for k, v in PROVIDER_BASE_URLS.items() if v]
