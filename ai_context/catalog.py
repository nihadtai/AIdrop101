"""Safe manifest and reader for the Markdown project-context library."""

from pathlib import Path
import re

CONTEXT_ROOT = Path(__file__).resolve().parent
def _metadata(path: Path) -> tuple[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = next((line.removeprefix("# ").strip() for line in lines if line.startswith("# ")), path.stem)
    description = next(
        (line.strip() for line in lines if line.strip() and not line.startswith("#") and not line.startswith("|")),
        "Project context reference.",
    )
    return title, description


def list_documents() -> list[dict]:
    paths = sorted(CONTEXT_ROOT.rglob("*.md"))
    id_by_path = {path.resolve(): path.relative_to(CONTEXT_ROOT).with_suffix("").as_posix() for path in paths}
    documents = []
    for path in paths:
        relative = path.relative_to(CONTEXT_ROOT)
        title, description = _metadata(path)
        content = path.read_text(encoding="utf-8")
        related = []
        for reference in re.findall(r"`([^`]+\.md)`", content):
            # References use project-context-root paths first, then local-relative paths.
            candidates = [CONTEXT_ROOT / reference, path.parent / reference]
            for candidate in candidates:
                target = candidate.resolve()
                if target in id_by_path and id_by_path[target] not in related:
                    related.append(id_by_path[target])
                    break
        documents.append({
            "id": relative.with_suffix("").as_posix(),
            "file": relative.as_posix(),
            "category": relative.parent.as_posix() if relative.parent != Path(".") else "foundation",
            "title": title,
            "description": description,
            "related": related,
        })
    return documents


def read_document(doc_id: str) -> dict:
    path = (CONTEXT_ROOT / f"{doc_id}.md").resolve()
    try:
        path.relative_to(CONTEXT_ROOT.resolve())
    except ValueError as error:
        raise KeyError(doc_id) from error
    if not path.is_file():
        raise KeyError(doc_id)
    title, description = _metadata(path)
    relative = path.relative_to(CONTEXT_ROOT)
    return {
        "id": doc_id,
        "file": relative.as_posix(),
        "category": relative.parent.as_posix() if relative.parent != Path(".") else "foundation",
        "title": title,
        "description": description,
        "content": path.read_text(encoding="utf-8"),
        "updated_at": path.stat().st_mtime,
    }
