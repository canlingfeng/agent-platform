"""知识库管理路由（Layer 3）：文档入库 / 列举 / 删除。

设计要点（多租户 + 安全）：
  - 所有端点 Depends(get_current_user) 拿已认证用户 user["id"]。
  - user_id 由服务端从 JWT 解出，绝不作为工具参数（IDOR 防护）。
  - 调用 core.rag_store 的 add_texts / list_docs / delete_doc，
    内部按 rag_user_{user_id} 物理隔离，与 Layer 2 rag_search 同源。
  - 这是直接 HTTP CRUD，不经过 Agent，所以用 get_current_user 而非 get_identity()。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from api.deps import get_current_user
from core.rag_store import add_texts, list_docs, delete_doc, RagError

router = APIRouter(prefix="/api/v1/rag", tags=["rag"])

class IngestRequest(BaseModel):
    text: str
    source: str = ""


@router.post("/ingest")
def ingest(req:IngestRequest,user=Depends(get_current_user)):
    try:
        doc_id=add_texts(user["id"], req.text, req.source)
    except RagError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"入库失败：{e}",
        )
    return {"doc_id": doc_id}


@router.get("/documents")
def list_documents(user=Depends(get_current_user)):
    docs = list_docs(user["id"])
    return docs

@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, user=Depends(get_current_user)):
    n = delete_doc(user["id"], doc_id)
    if n == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在或不属于你",
        )
    return {"deleted": n}