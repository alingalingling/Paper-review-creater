"""Read credentials through stdin; validate against DeepSeek and save locally."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trace_review.config import DEFAULT_BASE, DEFAULT_MODEL, save_model_config


def main():
    supplied = json.load(sys.stdin)
    config = {"base": DEFAULT_BASE, "model": DEFAULT_MODEL, "key": supplied["key"].strip()}
    request = urllib.request.Request(DEFAULT_BASE + "/models", headers={"Authorization": "Bearer " + config["key"]})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            models = [item["id"] for item in json.load(response).get("data", [])]
    except urllib.error.HTTPError as error:
        print(json.dumps({"ok": False, "http_status": error.code}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "Cannot connect to DeepSeek"}))
        return 1
    for name in (DEFAULT_MODEL, "deepseek-chat", "deepseek-v4-pro", "deepseek-reasoner"):
        if name in models:
            config["model"] = name
            break
    save_model_config(config)
    print(json.dumps({"ok": True, "model": config["model"], "available_models": models,
                      "storage": "Windows user DPAPI, outside source project"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
