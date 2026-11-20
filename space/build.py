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
dist = root / "dist"
dist.mkdir(exist_ok=True)
(dist / "index.html").write_text(html)
(dist / "README.md").write_text((root / "README.space.md").read_text())
print(f"wrote {dist/'index.html'} ({len(html)//1024} KB, {len(sources)} python modules)")
