"""RAG 向量检索层（Retrieval-Augmented Generation，检索增强生成）。

本模块只负责"检索"：把用户知识切块 → 向量化 → 存进向量库 → 按查询召回最相关片段 →
可选 rerank（重排序）提升精度。生成部分交给 LLM，检索结果作为上下文注入 prompt
（这一步在 core/tools.py 的 rag_search 工具里完成）。

为什么用 Milvus Lite
-------------------
Milvus Lite 是 Milvus 的本地文件模式：MilvusClient(uri=".../milvus.db") 一个文件
就是一座向量库，不需要单独跑 Milvus 服务进程。适合单机 / 容器单实例起步；将来要上
分布式，换 standalone / cluster 即可，Python API 完全一致（零迁移成本）。

多租户隔离
----------
每个用户一个独立 collection（rag_user_{user_id}）。即便检索时的 user_id 过滤逻辑
写错，用户 A 也绝不可能读到用户 B 的向量——物理隔离是最强隔离。

相似度度量（metric_type）
-------------------------
COSINE（余弦相似度）：与向量模长无关，只比方向。文本 embedding 通常已 L2 归一化，
此时 COSINE 等价于内积；Milvus 默认就是 COSINE，且"得分越大越相似"，与 bge-m3 一致。
"""
import os
import uuid
import requests
from core.models import resolve
# 项目根 = core/rag_store.py 上跳两级（parents[0]=core, parents[1]=项目根）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 向量库文件放 rag_data/（已在 .gitignore 排除），与 workspace/ 用户文件分开
_RAG_DIR = os.path.join(_PROJECT_ROOT, "rag_data")
os.makedirs(_RAG_DIR, exist_ok=True)
# 向量库地址：默认 Milvus Lite 本地文件模式；设置环境变量 MILVUS_URI 可切到
# Milvus standalone（宿主机 http://localhost:19530 / 容器内 http://milvus:19530）。
# 两种模式共用 MilvusClient，Python API 完全一致，切换零代码改动。
# 注：Windows 上 Milvus Lite 的 load_collection 会触发原生库访问违规(0xC0000005)，
# 故 Windows 本地验证一律走 standalone。
_DB_URI = os.environ.get("MILVUS_URI") or os.path.join(_RAG_DIR, "milvus.db")

# embedding 维度：BAAI/bge-m3 = 1024。建表维度必须和 embedding 模型输出一致，
# 以后换 embedding 模型若维度变了，这里要同步改。
_DIM = 1024
# 切块参数：chunk 太大检索粒度粗，太小语义不完整。中文段落常见长度，取经验值。
_CHUNK_SIZE = 512
_CHUNK_OVERLAP = 64

_RERANK_CANDIDATE_MULT = 3

# 空 filter 的 query 必须带 limit（Milvus 参数校验，否则报 code=1100），取服务端默认上限
_QUERY_LIMIT = 16384

class RagError(Exception):
    """RAG 检索层的可预期异常：缺密钥 / 上游报错 / 空库等，供上层转成友好提示。"""
    pass

_client=None  # MilvusClient 单例，懒加载

def _get_client():
    """懒加载 MilvusClient 单例。
     为什么懒加载：import 阶段不应触碰磁盘（测试 / 静态分析时不想真建文件）；
    首次真正检索 / 入库时才初始化。模块级单例避免重复打开同一个 db 文件句柄。
    """

    global _client
    if _client is None:
        from pymilvus import MilvusClient, DataType
        _client = MilvusClient(uri=_DB_URI)
    return _client


def _collection_name(user_id) -> str:
    """每用户一个 collection 名，物理隔离。"""
    return f"rag_user_{user_id}"

def _ensure_collection(user_id):
    """确保用户 collection 存在，不存在则创建。
    创建时指定参数：
    - dimension: embedding 维度
    - metric_type: 相似度度量，默认 COSINE，与 bge-m3 一致
    """
    from pymilvus import DataType   
    client=_get_client()     # 拿懒加载单例
    name=_collection_name(user_id)   # rag_user_{user_id}
    if client.has_collection(name):  # 已存在就直接返回，避免"已存在"报错
        return name
    schema=client.create_schema(auto_id=True,enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True,auto_id=True)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=_DIM)
    schema.add_field("text", DataType.VARCHAR, max_length=65535)
    schema.add_field("source", DataType.VARCHAR, max_length=512)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
    idx = client.prepare_index_params()                       # 准备索引参数容器
    idx.add_index("vector", index_type="AUTOINDEX", metric_type="COSINE")  # 向量字段必须显式 index_type
    client.create_collection(collection_name=name, schema=schema, index_params=idx)  
    return name

def embed(texts:list)->list:
    spec=resolve("embedding") # 调用 bge-m3 模型，输入文本列表，输出向量列表
    if not spec.api_key:
        raise RagError("没有 embedding API 密钥，请配置 ")
    resp=requests.post(
        spec.base_url+"/embeddings",
        headers={"Authorization": f"Bearer {spec.api_key}",
                "Content-Type": "application/json"},
        json={"model": spec.model, "input": texts, "encoding_format": "float"},
        timeout=60,
    )
    if  resp.status_code!=200: # 上游错误
        raise RagError(f"http状态码{resp.status_code}，上游报错：{resp.text}")
    return [item["embedding"] for item in resp.json()["data"]]   # 顺序与输入一致

def chunk_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]: 
    """把长文本切成可独立检索的片段（chunk）。
    算法：滑动窗口。窗口大小 chunk_size，相邻窗口重叠 overlap，步长 step=chunk_size-overlap。
    """
    text=text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    step=chunk_size-overlap
    chunk:list[str]=[]
    start =0
    if overlap >= chunk_size:
            step = chunk_size 
    while start<len(text):
        end=start+chunk_size
        piece = text[start:end]              
        chunk.append(piece)  
        start += step
    return chunk

def add_texts(user_id,text:str,source:str="")->str:
    """入库主入口：切块→向量化→写入用户 collection，返回该文档的 doc_id。"""
    doc_id = uuid.uuid4().hex # ① 文档级唯一 ID（32 位十六进制）
    name=_ensure_collection(user_id)  # ② 确保用户 collection 存在
    chunks=chunk_text(text)
    if not chunks:
        raise RagError("没有可检索文本")
    vectors = embed(chunks)     
    # ⑥ 组装：每行一个 dict，字段 = 建表时的 vector/text/source/doc_id
    # 用 zip 保证顺序一致；主键 id 由 Milvus auto_id 生成，禁止写进 dict
    data = [
        {"vector": v, "text": c, "source": source, "doc_id": doc_id}
        for c, v in zip(chunks, vectors)
    ]
    client = _get_client()
    client.insert(collection_name=name, data=data)  # ⑦ 批量插入
    client.flush(name)
    return doc_id                                   # ⑧ 返回 doc_id 供后续列举/删除

def rerank(query:str, candidates:list[dict],top_k:int)->list[dict]:
    """用交叉编码器对候选重排序，返回前 top_k（精度高于纯向量余弦）。"""
    if not candidates:
        return []
    spec=resolve("rerank")
    if not spec.api_key:
        raise RagError("没有 rerank API 密钥，请配置 ")
    resp=requests.post(
        spec.base_url + "/rerank",
        headers={"Authorization": f"Bearer {spec.api_key}", "Content-Type": "application/json"},
        json={
            "model": spec.model,
            "query": query,
            "documents": [c["text"] for c in candidates],  # API 只收字符串列表，最少 1 条
            "top_n": top_k,
            "return_documents": False,  
        },
        timeout=60,
     )
    if resp.status_code != 200:
        raise RagError(f"rerank 上游错误 {resp.status_code}: {resp.text}")
    ranked=resp.json()["results"]
    out=[]
    for r in ranked:
        c=dict(candidates[r["index"]])
        c["score"]=r["relevance_score"]
        out.append(c)
    return out
     

def search(user_id, query: str, top_k: int = 5, use_rerank: bool = True) -> list[dict]:
    """检索主入口：查询向量化 → 向量召回（多召回候选）→ 可选 rerank 精排 → 返回 top_k。"""
    name = _collection_name(user_id)
    client = _get_client()
    if not client.has_collection(name):
        return []
    q_vec = embed([query])[0]
    candidate_limit = top_k * _RERANK_CANDIDATE_MULT   # 多召回候选，交给 rerank 精排
    client.load_collection(name)                       # Milvus：集合必须先加载到内存才能检索
    res = client.search(
        collection_name=name,
        data=[q_vec],
        limit=candidate_limit,
        output_fields=["text", "source", "doc_id"],
    )
    hits = res[0]
    candidates = [
        {"text": h["text"], "source": h["source"], "doc_id": h["doc_id"], "score": h["distance"]}
        for h in hits
    ]
    if use_rerank and candidates:
        return rerank(query, candidates, top_k)        # 精排取前 top_k
    return candidates[:top_k]                          # 无 rerank 时退回纯向量分数

def list_docs(user_id) -> list[dict]:
    """列出该用户所有文档（按 doc_id 去重，返回 doc_id + source）。"""
    name = _collection_name(user_id)
    client = _get_client()
    if not client.has_collection(name):
        return []
    client.load_collection(name)                       # 同上，query 前也要加载
    rows = client.query(
        collection_name=name,
        filter="",
        output_fields=["doc_id", "source"],
        limit=_QUERY_LIMIT,          # 空 expression 必须配 limit，否则报 code=1100
    )
    seen = {}
    for r in rows:
        did = r["doc_id"]
        if did not in seen:
            seen[did] = {"doc_id": did, "source": r.get("source", "")}
    return list(seen.values())

def delete_doc(user_id, doc_id: str) -> int:
    """按 doc_id 物理删除该文档的全部 chunk，返回删除条数。"""
    name = _collection_name(user_id)
    client = _get_client()
    if not client.has_collection(name):
        return 0
    # Milvus delete 用 filter 表达式；字符串值必须加双引号包裹
    res = client.delete(collection_name=name, filter=f'doc_id == "{doc_id}"')
    # delete 只写删除缓冲，flush 后才真正对后续 query 可见（实测不 flush 立即查数据还在）
    client.flush(name)
    # 返回值是 OmitZeroDict（dict 子类），键为 delete_count；dict 取键用 .get()，不能用 getattr
    return res.get("delete_count", 0)