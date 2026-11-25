"""Standalone, lightweight server for the AI Context Brain graph."""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ai_context import get_context_brain
from ai_context.catalog import list_documents, read_document

app = FastAPI(title="AI Context Brain", version="1.0.0")


@app.get("/", response_class=HTMLResponse)
@app.get("/ai-context", response_class=HTMLResponse)
async def brain_home() -> HTMLResponse:
    from pathlib import Path

    template = Path(__file__).resolve().parent / "templates" / "ai_context.html"
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.get("/api/ai-context/documents")
async def brain_documents() -> dict:
    return {"documents": list_documents()}


@app.get("/api/ai-context/documents/{document_id:path}")
async def brain_document(document_id: str) -> dict:
    try:
        return read_document(document_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown AI context document")


@app.get("/api/ai-context/telemetry")
async def brain_telemetry() -> dict:
    return get_context_brain().snapshot()
