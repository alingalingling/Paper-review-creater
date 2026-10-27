import argparse
import json
from pathlib import Path

from .core import export_run, make_run, read_documents


def main():
    parser = argparse.ArgumentParser(description="循证综述：限定文献集逐句证据账本")
    parser.add_argument("--input", type=Path, help="文献目录（PDF、UTF-8 TXT/MD）")
    parser.add_argument("--topic", default="反馈 学习效果")
    parser.add_argument("--requirements", default="", help="写作要求，例如不少于800字")
    parser.add_argument("--mode", choices=["extract", "model"], default="extract")
    parser.add_argument("--out", type=Path, default=Path("outputs/demo"))
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    folder = args.input or Path(__file__).resolve().parents[1] / "data/demo"
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".txt", ".md", ".pdf"))
    if not files:
        parser.error("目录中没有可导入的文献。")
    try:
        run = make_run(read_documents(files), args.topic, args.mode, max(1, args.limit), requirements=args.requirements)
        print(export_run(run, args.out))
        print(json.dumps({"sentences": len(run["claims"]), "gaps": run["gaps"]}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"失败：{exc}\n")


if __name__ == "__main__":
    main()
