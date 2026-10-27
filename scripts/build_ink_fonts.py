"""Build tiny, self-hosted OFL font subsets for the background (development only)."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
FONTS = (
    ("mashanzheng", "MaShanZheng-Regular.ttf", "kai", "Trace Ink Kai"),
    ("zhimangxing", "ZhiMangXing-Regular.ttf", "running", "Trace Ink Running"),
    ("liujianmaocao", "LiuJianMaoCao-Regular.ttf", "grass", "Trace Ink Grass"),
    ("zcoolxiaowei", "ZCOOLXiaoWei-Regular.ttf", "seal", "Trace Ink Display"),
)


def build(entry):
    folder, filename, short, family = entry
    base = f"https://raw.githubusercontent.com/google/fonts/main/ofl/{folder}/"
    cache = ROOT / "outputs" / "font-sources"
    cache.mkdir(parents=True, exist_ok=True)
    def download(name):
        target = cache / f"{folder}-{name}"
        if not target.exists():
            with urllib.request.urlopen(base + name, timeout=45) as response:
                target.write_bytes(response.read())
        return target.read_bytes()
    raw, license_text = download(filename), download("OFL.txt")
    assert b"SIL OPEN FONT LICENSE" in license_text
    font = TTFont(io.BytesIO(raw))
    text = json.loads(re.search(r'const glyphs = Array.from\(("[^"]+")\)',
                               (ROOT / "web/atmosphere.js").read_text("utf-8"))[1])
    characters = set(text + "循证")
    missing = characters - {chr(cp) for cp in font.getBestCmap()}
    if missing:
        raise ValueError(f"{family} is missing {sorted(missing)}")
    options = subset.Options()
    options.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14, 16, 17]
    options.name_legacy = True
    options.name_languages = [0x409]
    subsetter = subset.Subsetter(options)
    subsetter.populate(text="".join(sorted(characters)))
    subsetter.subset(font)
    names = {1:family, 2:"Regular", 3:family+" 1.0", 4:family,
             6:family.replace(" ", "")+"-Regular", 16:family, 17:"Regular"}
    for name_id, value in names.items():
        font["name"].setName(value, name_id, 3, 1, 0x409)
    font.flavor = "woff2"
    destination = ROOT / "web/fonts" / f"trace-ink-{short}.woff2"
    font.save(destination)
    (destination.parent / f"OFL-{folder}.txt").write_bytes(license_text)
    return {"file":destination.name, "family":family, "upstream":base+filename,
            "source_sha256":hashlib.sha256(raw).hexdigest(), "glyphs":len(characters),
            "bytes":destination.stat().st_size}


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=4) as pool:
        built = list(pool.map(build, FONTS))
    (ROOT / "web/fonts/ink-manifest.json").write_text(json.dumps(built, indent=2), "utf-8")
    print(json.dumps(built, indent=2))
