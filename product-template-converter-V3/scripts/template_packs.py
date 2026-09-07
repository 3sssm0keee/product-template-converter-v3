from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delivery_contract import get_profile, target_values


DEFAULT_PACK_ID = "zxty-fixed-v1"




def _validate_delivery_targets(config: dict[str, Any]) -> None:
    declared = config.get("delivery_targets")
    if not isinstance(declared, dict):
        raise ValueError("template pack delivery_targets must be an object")

    for target in target_values():
        profile = get_profile(target)
        target_config = declared.get(target)
        if not isinstance(target_config, dict):
            raise ValueError(f"template pack delivery_targets missing {target}")
        if target_config.get("delivery_format") != profile.delivery_format:
            raise ValueError(f"template pack delivery_targets {target} conflicts with delivery contract")


# A-ABL-03：模块退役涉及 test_template_packs.py 的 3 项旧契约测试。
# 保留 template_catalog_v3.py；删除旧测试不能视为原验收等价。
def load_template_pack(skill_root: Path, pack_id: str) -> dict[str, Any]:
    pack_dir = skill_root / "template_packs" / pack_id
    config = json.loads((pack_dir / "template.json").read_text(encoding="utf-8"))
    _validate_delivery_targets(config)
    template = (pack_dir / config["template"]).resolve()
    if not template.is_file():
        raise ValueError(f"template asset not found: {template}")
    return {**config, "pack_dir": str(pack_dir), "template_path": str(template)}
