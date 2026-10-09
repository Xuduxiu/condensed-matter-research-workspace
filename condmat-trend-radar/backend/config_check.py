from __future__ import annotations

import json

from backend.config import (
    data_dir,
    db_path,
    deepseek_api_key,
    deepseek_base_url,
    deepseek_model_fast,
    deepseek_model_pro,
    ensure_data_layout,
    mask_secret,
    openalex_api_key,
    openalex_mailto,
)


def build_status() -> dict[str, object]:
    layout = ensure_data_layout()
    openalex_key = openalex_api_key()
    deepseek_key = deepseek_api_key()
    return {
        "data_dir": str(data_dir()),
        "db_path": str(db_path()),
        "layout": layout,
        "openalex": {
            "has_key": bool(openalex_key),
            "key_masked": mask_secret(openalex_key),
            "mailto_configured": bool(openalex_mailto()),
        },
        "deepseek": {
            "has_key": bool(deepseek_key),
            "key_masked": mask_secret(deepseek_key),
            "base_url": deepseek_base_url(),
            "model_fast": deepseek_model_fast(),
            "model_pro": deepseek_model_pro(),
        },
    }


def main() -> dict[str, object]:
    status = build_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    main()