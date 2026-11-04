"""Local, repeatable graphic/dialogue typesetting; integrated scene text stays in the image provider."""
from __future__ import annotations

import json
import math
import os
import csv
import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def inspect_text(path: Path, expected: list[str], *, regions: list[dict] | None = None) -> dict:
    tool = shutil.which("tesseract")
    if not tool:
        return {"status":"UNVERIFIED","reason":"ocr_not_installed","expected":expected}
    def read(image_path, mode):
        run=subprocess.run([tool,str(image_path),"stdout","--psm",str(mode),"tsv"],capture_output=True,text=True,timeout=30)
        if run.returncode:raise RuntimeError("ocr_failed")
        return [r for r in csv.DictReader(io.StringIO(run.stdout),delimiter="\t") if r.get("text","").strip()]
    background=[]
    try:
        full_rows=read(path,11)
        rows=full_rows
        if regions:
            with Image.open(path) as original, tempfile.TemporaryDirectory(prefix="infenergy-text-qa-") as temp:
                boxes=[_box(layer["rect"],original.size) for layer in regions]
                rows=[]
                for index,box in enumerate(boxes):
                    crop=Path(temp)/(str(index)+".png");original.crop(box).save(crop,"PNG")
                    rows.extend(read(crop,6))
                for row in full_rows:
                    x=float(row["left"])+float(row["width"])/2;y=float(row["top"])+float(row["height"])/2
                    if not any(b[0]<=x<=b[2] and b[1]<=y<=b[3] for b in boxes):background.append(row)
    except subprocess.TimeoutExpired:
        return {"status":"UNVERIFIED","reason":"ocr_timeout","expected":expected}
    except RuntimeError:
        return {"status":"UNVERIFIED","reason":"ocr_failed","expected":expected}
    from collections import Counter
    target=Counter(words(" ".join(expected)))
    actual=Counter(words(" ".join(r["text"] for r in rows)))
    missing=list((target-actual).elements())
    confident=Counter(word for r in rows if float(r.get("conf",0))>=70 for word in words(r["text"]))
    extra=list(Counter({word:count for word,count in (actual-target).items() if word in target}).elements())
    extra.extend(word for word in confident.elements() if word not in target)
    uncertain=[{"text":r["text"],"confidence":float(r["conf"])} for r in rows if float(r.get("conf",0))<70 and any(w not in target for w in words(r["text"]))]
    background_review=[]
    for row in background:
        tokens=words(row["text"]);confidence=float(row["conf"])
        significant=[w for w in tokens if len(w)>1]
        if confidence>=70 and significant:extra.extend(significant)
        elif tokens:background_review.append({"text":row["text"],"confidence":confidence,"reason":"background_shape_requires_visual_review"})
    return {"status":"PASS" if not missing and not extra else "REVIEW_REQUIRED","missing_words":missing,
            "unexpected_or_duplicate_words":extra,"expected":expected,"observed":" ".join(r["text"] for r in rows),
            "inspection_mode":"controlled_regions_and_full_background" if regions else "full_image",
            "low_confidence_candidates":uncertain,"background_review_candidates":background_review}


def _box(rect, size, *, safe_area=True):
    if not isinstance(rect, list) or len(rect) != 4:
        raise ValueError("normalized_xywh_required")
    x,y,w,h = [float(v) for v in rect]
    if not all(math.isfinite(v) for v in [x,y,w,h]): raise ValueError("finite_coordinates_required")
    margin=.04 if safe_area else 0
    if x < margin or y < margin or w <= 0 or h <= 0 or x+w > 1-margin or y+h > 1-margin:
        raise ValueError("text_outside_safe_area")
    return tuple(int(v) for v in (x*size[0], y*size[1], (x+w)*size[0], (y+h)*size[1]))


def render(source: Path, output: Path, layers: list[dict], *, protected: list[list] = (), aspect_ratio: str | None = None) -> dict:
    if not layers or len(layers)>8: raise ValueError("one_to_eight_text_layers_required")
    with Image.open(source) as original:
        image = original.convert("RGB")
    if aspect_ratio:
        if min(image.size)<720:raise ValueError("source_artwork_resolution_too_low")
        from production_images import normalize
        image=normalize(image,aspect_ratio)
    draw = ImageDraw.Draw(image); width,height = image.size
    protected_boxes = [_box(r, image.size,safe_area=False) for r in protected]
    occupied=[]; manifest=[]
    font_path = os.environ.get("INFENERGY_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if not Path(font_path).is_file() and shutil.which("fc-match"):
        found=subprocess.run(["fc-match","-f","%{file}","DejaVu Sans:style=Bold"],capture_output=True,text=True,timeout=5)
        if found.returncode==0:font_path=found.stdout.strip()
    if not Path(font_path).is_file(): raise ValueError("approved_font_not_installed")
    for layer in layers:
        text = str(layer.get("text", "")).strip()
        if not text or len(text)>300: raise ValueError("text_required_max_300")
        box = _box(layer.get("rect"), image.size); x,y,right,bottom=box
        for p in [*protected_boxes,*occupied]:
            if x<p[2] and right>p[0] and y<p[3] and bottom>p[1]: raise ValueError("text_overlaps_protected_region_or_other_text")
        occupied.append(box)
        style=layer.get("style","headline")
        if style not in {"headline","dialogue","label"}: raise ValueError("unknown_text_style")
        minimum=max(20,round(width*(.034 if style=="dialogue" else .04)))
        padding=max(8,round(width*.015)); max_width=right-x-2*padding
        fitted=None
        for size in range(max(minimum,round(width*.075)), minimum-1,-1):
            font=ImageFont.truetype(font_path,size); lines=[]; current=""
            for word in text.split():
                candidate=(current+" "+word).strip()
                if draw.textlength(candidate,font=font)<=max_width: current=candidate
                else:
                    if not current: break
                    lines.append(current); current=word
            else:
                if current: lines.append(current)
                line_h=round(size*1.25)
                if all(draw.textlength(line,font=font)<=max_width for line in lines) and len(lines)*line_h<=bottom-y-2*padding:
                    fitted=(font,lines,line_h,size); break
        if fitted is None: raise ValueError("text_does_not_fit_at_readable_size_shorten_or_resize")
        font,lines,line_h,size=fitted
        # Contrast is deterministic, including on a complex photographic background.
        if style=="dialogue": draw.rounded_rectangle(box,radius=round(width*.025),fill="white",outline="#10212b",width=3)
        else: draw.rounded_rectangle(box,radius=round(width*.012),fill="#10212b")
        color="#10212b" if style=="dialogue" else "white"
        for i,line in enumerate(lines): draw.text((x+padding,y+padding+i*line_h),line,fill=color,font=font)
        manifest.append({"text":text,"rect":layer["rect"],"style":style,"font_size":size,"lines":lines})
    output.parent.mkdir(parents=True,exist_ok=True); image.save(output,"PNG")
    result={"mode":"controlled","size":[width,height],"layers":manifest,"ocr":inspect_text(output,[x["text"] for x in manifest],regions=manifest),"human_mobile_review_required":True}
    output.with_suffix(".typography.json").write_text(json.dumps(result,indent=2))
    return result
