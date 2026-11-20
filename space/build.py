"""Build the Hugging Face static Space: inline promo_finder/*.py into one self-contained index.html.

    python space/build.py      ->  space/dist/index.html + space/dist/README.md
The page runs the *same* Python package in the visitor's browser (Pyodide in a Web Worker).
No server, no stored keys, no screenplays in the Space.
"""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
pkg = root.parent / "promo_finder"
sources = {p.name: p.read_text() for p in sorted(pkg.glob("*.py"))}
blob = json.dumps(sources).replace("</", "<\\/")          # safe inside <script>
html = (root / "index.template.html").read_text().replace("/*__PY_SOURCES__*/", blob)

# Optional pre-rendered previews (outputs/ is git-ignored, so clips never reach GitHub; they only go into the private Space)
import base64
samples = []
prev = root.parent / "outputs" / "previews"
if (prev / "manifest.json").exists():
    for m in json.loads((prev / "manifest.json").read_text()):
        clips = {k: "data:video/mp4;base64," + base64.b64encode((prev / f).read_bytes()).decode()
                 for k, f in m.get("clips", {}).items() if (prev / f).exists()}
        if clips:
            samples.append({**m, "clips": clips})
html = html.replace("/*__SAMPLES__*/", json.dumps(samples).replace("</", "<\\/"))
dist = root / "dist"
dist.mkdir(exist_ok=True)
(dist / "index.html").write_text(html)
# Space README = Hugging Face settings header + the exact same README as GitHub
(dist / "README.md").write_text((root / "space_header.yml").read_text() + "\n" + (root.parent / "README.md").read_text())
print(f"wrote {dist/'index.html'} ({len(html)//1024} KB, {len(sources)} python modules, {len(samples)} pre-rendered previews)")
