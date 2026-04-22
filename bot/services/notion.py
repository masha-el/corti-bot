import os
import logging

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

logger = logging.getLogger(__name__)

def _get_client() -> Client:
    return Client(auth=os.environ["NOTION_TOKEN"])

def search_pages(query: str) -> list[dict]:
    client = _get_client()

    results = client.search(
        query=query,
        filter={"value": "page", "property": "object"},
        sort={"direction": "descending", "timestamp": "last_edited_time"},
    ).get("results", [])

    pages = []
    for page in results[:10]:
        title = _extract_title(page)
        pages.append({
            "id":    page["id"],
            "title": title,
            "url":   page.get("url", ""),
        })
    return pages

def get_page_content(page_id: str) -> str:
    """Fetches all text content from a page's blocks."""
    client = _get_client()

    blocks = client.blocks.children.list(block_id=page_id).get("results", [])
    lines = []

    for block in blocks:
        block_type = block["type"]
        block_data = block.get(block_type, {})
        rich_text = block_data.get("rich_text", [])
        text = "".join(t["plain_text"] for t in rich_text)

        if text:
            lines.append(text)

    return "\n".join(lines) if lines else "Page is empty."

def create_page(title: str, content: str, parent_id: str = None) -> str:
    """Creates a new page and returns its URL."""
    client = _get_client()
    resolved_parent_id = parent_id or os.environ.get("NOTION_PARENT_PAGE_ID")

    parent = (
        {"type": "page_id", "page_id": resolved_parent_id}
        if resolved_parent_id
        else {"type": "workspace", "workspace": True}
    )

    page = client.pages.create(
        parent=parent,
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                },
            }
        ],
    )
    return page["url"]

def _extract_title(page: dict) -> str:
    """Extracts title from a page object — handles both page and database_page types."""
    props = page.get("properties", {})

    # common title property names
    for key in ("title", "Name", "Title"):
        prop = props.get(key)
        if prop and prop.get("title"):
            return "".join(t["plain_text"] for t in prop["title"])
        
    return "Untitled"

def get_top_level_pages() -> list[dict]:
    client = _get_client()

    results = client.search(
        filter={"value": "page", "property": "object"},
        sort={"direction": "descending", "timestamp": "last_edited_time"},
    ).get("results", [])

    pages = []
    for page in results[:10]:
        title = _extract_title(page)
        if title and title != "Untitled":
            pages.append({
                "id":    page["id"],
                "title": title,
            })
    
    return pages

