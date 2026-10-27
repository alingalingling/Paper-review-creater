"""Package source by allowlist, excluding credentials, workspaces and exports."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from sys import path as module_path
ROOT = Path(__file__).resolve().parents[1]
module_path.insert(0, str(ROOT))
from trace_review.config import load_model_config


def main():
    selected = [p for p in ROOT.iterdir() if p.is_file() and p.suffix in (".py", ".md", ".cmd", ".txt")]
    selected.append(ROOT / ".gitignore")
    for directory in ("trace_review", "web", "tests", "scripts", "data/demo"):
        selected.extend(p for p in (ROOT / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    secret = load_model_config().get("key", "").encode()
    archive = ROOT.parent / "循证综述-源码.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as zipped:
        for file in sorted(set(selected)):
            if file.suffix in (".dpapi", ".bin") or file.name.startswith(".env"):
                continue
            data = file.read_bytes()
            if secret and secret in data:
                raise RuntimeError("源码中发现密钥，已停止打包。")
            zipped.writestr(str(Path(ROOT.name) / file.relative_to(ROOT)), data)
    print("Source archive updated; credential scan passed.")


if __name__ == "__main__":
    main()
