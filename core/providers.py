
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