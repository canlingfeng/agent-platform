
"""LLM 供应商预设目录：把"选哪家"从代码里抽出来，变成数据。

所有预设都走 OpenAI 兼容接口（POST {base_url}/chat/completions，Bearer 鉴权）——
这是 2024 年后的事实标准：DeepSeek / 火山方舟 / 阿里百炼 / Moonshot / Anthropic
都提供兼容层，所以 ChatOpenAI 一个客户端全打通，不用给每家写 SDK。

⚠️ 模型名会随各家发版过期（如 claude-sonnet-4-5、deepseek-chat），
以各自官方文档为准；预设只是常用起点，"自定义"入口就是为追新准备的。
"""

PRESETS = {
    "qwen_vl": {
        "label": "Qwen 视觉模型",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
        "supports_images": True,
        "supports_video": False,
    },
    "modelscope": {
        "label": "魔搭 ModelScope（Qwen）",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "model": "Qwen/Qwen3.5-27B",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "doubao": {
        "label": "豆包 火山方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        # 方舟的"模型名"实际是接入点 ID（ep-xxxxxx），在控制台创建推理接入点后复制
        "model": "doubao-pro-32k",
    },
    "openai": {
        "label": "GPT OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "claude": {
        "label": "Claude Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-5",
    },
    "qwen": {
        "label": "千问 阿里百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "kimi": {
        "label": "Kimi Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "custom": {
        "label": "自定义（手填 Base URL + 模型名）",
        "base_url": "",
        "model": "",
    },
}


# ---- 能力层（capability layer）------------------------------------------
# 背景：RAG 需要 embedding / rerank，这两类模型和"对话模型"不是一回事，
# 且往往来自不同厂商（本项目：对话走魔搭，向量化走硅基流动）。所以除了
# 上面那份"对话供应商目录"，还需要一张按【能力】索引的表。

# 厂商 → OpenAI 兼容 base_url。对话厂商的地址直接由 PRESETS 派生（不重复写，
# 避免同一 URL 两处维护），再补上只提供 embedding/rerank 的厂商。
PROVIDER_BASE_URLS = {k: v["base_url"] for k, v in PRESETS.items() if v.get("base_url")}
PROVIDER_BASE_URLS.update({
    "siliconflow": "https://api.siliconflow.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
})

# 能力 → 默认 provider / 模型 / 密钥环境变量名。
#   - config.json 的 models.<capability> 可覆盖 provider 与 model
#   - 密钥只认环境变量，config 里**不存明文**（上线约束：无明文密钥落盘）
#   - custom 例外：自定义端点没有对应环境变量，密钥可落在 config.json
CAPABILITY_DEFAULTS = {
    "chat": {
        "provider": "modelscope",
        "model": "Qwen/Qwen3.5-27B",
        "api_key_env": "MODELSCOPE_API_KEY",
    },
    "embedding": {
        "provider": "siliconflow",
        "model": "BAAI/bge-m3",
        "api_key_env": "SILICONFLOW_API_KEY",
    },
    "rerank": {
        "provider": "siliconflow",
        "model": "BAAI/bge-reranker-v2-m3",
        "api_key_env": "SILICONFLOW_API_KEY",
    },
}


def provider_capabilities(provider: str) -> list:
    """该厂商提供哪些能力。PRESETS 里的都是对话厂商，故默认 ["chat"]。

    新增只做 embedding 的厂商时不会出现在 PRESETS 里，也就不会被
    GET /api/v1/providers 返回给前端——避免用户在模型选择器里选到不能聊天的模型。
    """
    return list(PRESETS.get(provider, {}).get("capabilities", ["chat"]))


def resolve(provider: str, base_url: str = "", model: str = "") -> dict:
    """把 UI 选择解析成 {"base_url", "model"}：
    - 预设名 → 取目录里的值
    - "custom" → 用调用方传进来的手填值
    - 未知值 → 回落 modelscope（防御性，别让脏配置把 Agent 打到空地址上）
    """
    preset = PRESETS.get(provider)
    if preset is None:
        provider, preset = "modelscope", PRESETS["modelscope"]
    if provider == "custom":
        return {"base_url": base_url.strip(), "model": model.strip()}
    return {"base_url": preset["base_url"], "model": preset["model"]}

def model_supports_images(provider: str) -> bool:
    """当前模型是否支持图片输入。缺字段默认 False，绝不抛 KeyError。"""
    return bool(PRESETS.get(provider, {}).get("supports_images", False))