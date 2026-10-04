"""The one endpoint restore mover pods call. It is authenticated by the
single-use token in the pod, not by a user session."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...config import get_settings
from ...kube.mover import claim_channel
from ..deps import audit, get_db

router = APIRouter(tags=["restore mover"], include_in_schema=False)


@router.get("/api/mover/{cid}")
async def stream(cid: str, request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    fifo = claim_channel(get_settings().data_dir, cid, token) if token else None
    if fifo is None:
        audit(db, request, "mover.stream", target=cid[:12], success=False)
        db.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such stream")
    audit(db, request, "mover.stream", target=cid[:12])
    db.commit()
    # Opening the read end waits for the worker to start writing.
    f = await run_in_threadpool(open, fifo, "rb")

    def chunks():
        try:
            while data := f.read(1 << 20):
                yield data
        finally:
            f.close()

    return StreamingResponse(chunks(), media_type="application/octet-stream")
