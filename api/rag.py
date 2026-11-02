"""知识库管理路由（Layer 3）：文档入库 / 列举 / 删除。

设计要点（多租户 + 安全）：
  - 所有端点 Depends(get_current_user) 拿已认证用户 user["id"]。
  - user_id 由服务端从 JWT 解出，绝不作为工具参数（IDOR 防护）。
  - 调用 core.rag_store 的 add_texts / list_docs / delete_doc，
    内部按 rag_user_{user_id} 物理隔离，与 Layer 2 rag_search 同源。
  - 这是直接 HTTP CRUD，不经过 Agent，所以用 get_current_user 而非 get_identity()。
"""
import pathlib

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from api.deps import get_current_user
from core.rag_store import add_texts, list_docs, delete_doc, RagError

router = APIRouter(prefix="/api/v1/rag", tags=["rag"])

_ALLOWED_EXT = {".txt", ".md", ".markdown", ".log", ".csv"}
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5MB 文本约 250 万字，够覆盖手册/笔记


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


@router.post("/upload")
def upload_file(file: UploadFile = File(...), user=Depends(get_current_user)):
    """上传文本文件入库，支持 .txt/.md/.log/.csv，最大 5MB。
    Args:
        file: multipart/form-data 里的文件字段，字段名必须是 file
    Returns:
        {"doc_id": ..., "source": 原始文件名, "chars": 字符数}
    """
    name = pathlib.Path(file.filename or "").name
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少文件名")

    ext = pathlib.Path(name).suffix.lower()
    if ext not in _ALLOWED_EXT:
        allowed = " / ".join(sorted(_ALLOWED_EXT))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型 {ext or '(无扩展名)'}，仅支持 {allowed}",
        )

    # 只读"上限+1"个字节：
    #   ① UploadFile.read() 是 async 方法，本端点是 def（跑在线程池），拿不到 await，
    #      所以用底层同步句柄 file.file（SpooledTemporaryFile）；
    #   ② 多读 1 字节就足以判定"超限"，不必把 500MB 文件整块读进内存。
    raw = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限",
        )
    if not raw.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件是空的")

    # 编码兜底：先 UTF-8（utf-8-sig 顺带吃掉 Windows 记事本加的 BOM），失败再试 GBK；
    # 两者都失败说明不是文本文件（可能是二进制改后缀），直接拒绝而不是塞乱码进向量库
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无法识别文件编码，请另存为 UTF-8 后重试",
            )

    if not text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件解码后没有有效内容")

    try:
        doc_id = add_texts(user["id"], text, name)
    except RagError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"入库失败：{e}",
        )

    return {"doc_id": doc_id, "source": name, "chars": len(text)}