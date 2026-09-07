"""Deterministic, disabled annotation port reserved for V4."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


SCHEMA_VERSION = "review-annotation-port-v3"
INTERFACE_VERSION = "1.0.0"
STATUS = "RESERVED_FOR_V4"
CAPABILITIES = (
    "text_selection", "threaded_comments", "status_replies",
    "completion_labels", "local_persistence", "remote_sync",
)
STORAGE_METHODS = ("load", "save", "export", "import", "subscribe")
ACTOR_CAPABILITIES = {
    "user": ("create", "edit", "delete", "complete", "reopen"),
    "codex": ("reply_status",),
    "agent": ("reply_status",),
}


def _targets(queue: dict[str, Any]) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for item in queue.get("items", []):
        item_id = str(item.get("review_item_id", ""))
        targets.append({"target_id": f"review-item:{item_id}", "target_kind": "review_item"})
    preview = queue.get("document_preview") or {}
    for page_index, page in enumerate(preview.get("pages", []), 1):
        for section_index, section in enumerate(page.get("sections", []), 1):
            for block_index, block in enumerate(section.get("blocks", []), 1):
                base = f"{page_index}:{section_index}:{block_index}"
                targets.append({"target_id": f"preview-block:{base}", "target_kind": "preview_block"})
                kind = str(block.get("kind", ""))
                if kind == "text":
                    targets.append({"target_id": f"preview-text:{base}", "target_kind": "preview_text"})
                if isinstance(block.get("image"), dict) and block["image"].get("path"):
                    targets.append({"target_id": f"preview-image:{base}", "target_kind": "preview_image"})
                if block.get("table_rows"):
                    targets.append({"target_id": f"preview-table:{base}", "target_kind": "preview_table"})
    return targets


def build_review_annotation_port(queue: dict[str, Any]) -> dict[str, Any]:
    """Build the V3 machine port description without reviewer identity or receipt data."""
    return {
        "schema_version": SCHEMA_VERSION,
        "interface_version": INTERFACE_VERSION,
        "review_id": str(queue.get("review_id") or ""),
        "status": STATUS,
        "enabled": False,
        "capabilities": {name: False for name in CAPABILITIES},
        "storage_methods": list(STORAGE_METHODS),
        "actor_capabilities": {actor: list(values) for actor, values in ACTOR_CAPABILITIES.items()},
        "targets": deepcopy(_targets(queue)),
    }
