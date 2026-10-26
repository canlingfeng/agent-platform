"""Embedding 链路验证：文本 → 向量 → 余弦相似度。

跑通说明"向量化"这一层能用，下一步才和 Milvus 拼成 RAG。

配置来源统一走 core.models.resolve("embedding")，三层优先级：
环境变量 > config.json 的 models.embedding > CAPABILITY_DEFAULTS。
密钥只从环境变量读（SILICONFLOW_API_KEY），脚本里不出现任何模型名与地址。
"""
import os
import sys

import requests

# 让脚本能 import 项目内的 core.* 包（脚本在 tests/ 下，项目根在上一级）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import resolve  # noqa: E402

# 三条测试句：第 1、2 条语义接近，第 3 条无关（作对照组）
SENTENCES = [
    "今天天气很好，适合出门散步",
    "外面阳光明媚，适合外出走走",
    "MySQL 的主键设计要避免页分裂",
]

spec = resolve("embedding")
print(f"capability = {spec.capability}")
print(f"provider   = {spec.provider}")
print(f"base_url   = {spec.base_url}")
print(f"model      = {spec.model}")
print(f"api_key    = {'已配置' if spec.api_key else '缺失'}")   # 密钥本身绝不打印

if not spec.api_key:
    print("\n缺少密钥：请设置环境变量 SILICONFLOW_API_KEY 后重跑。")
    raise SystemExit(1)

resp = requests.post(
    spec.base_url + "/embeddings",
    headers={"Authorization": f"Bearer {spec.api_key}",
             "Content-Type": "application/json"},
    json={"model": spec.model, "input": SENTENCES, "encoding_format": "float"},
    timeout=60,
)
print("\nHTTP 状态码:", resp.status_code)
if resp.status_code != 200:
    print("出错，服务端返回:", resp.text[:500])
    raise SystemExit(1)

vecs = [item["embedding"] for item in resp.json()["data"]]
print(f"拿到 {len(vecs)} 条向量，每条维度 = {len(vecs[0])}")


def cosine(a, b):
    """余弦相似度 = 点积 / (模长a × 模长b)，取值 [-1, 1]，越大越相似。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:      # 零向量没有方向，防御除零
        return 0.0
    return dot / (na * nb)


print(f"句1 vs 句2（语义接近）: {cosine(vecs[0], vecs[1]):.4f}")
print(f"句1 vs 句3（无关）    : {cosine(vecs[0], vecs[2]):.4f}")
print(f"句2 vs 句3（无关）    : {cosine(vecs[1], vecs[2]):.4f}")
