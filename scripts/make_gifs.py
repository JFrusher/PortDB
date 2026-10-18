"""Assemble the frames from capture.js into the README GIFs.

    python scripts/make_gifs.py [frames_dir] [out_dir]      # needs Pillow
"""

import sys
from pathlib import Path

from PIL import Image

FRAMES = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/media/frames")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/media")
SCENES = {               # gif -> frame folders, played in order
    "hero": ["hero"], "closeup": ["closeup"], "xray": ["xray"], "rehandle": ["rehandle"],
    "landside": ["rail", "customs"], "replay": ["replay"], "storm": ["storm"],
}
WIDTH = 880


def frames(folder: Path):
    for p in sorted(folder.glob("*.png")):
        im = Image.open(p).convert("RGB")
        im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
        yield im.quantize(colors=192, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


for gif, folders in SCENES.items():
    ims = [im for f in folders if (FRAMES / f).is_dir() for im in frames(FRAMES / f)]
    if not ims:
        continue
    ims[0].save(OUT / f"{gif}.gif", save_all=True, append_images=ims[1:], duration=90, loop=0, optimize=True)
    print(f"{gif}.gif: {len(ims)} frames, {(OUT / f'{gif}.gif').stat().st_size // 1024} KB")
