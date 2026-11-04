"""Normalize provider artwork before review, preserving the complete composition."""
from __future__ import annotations
from PIL import Image, ImageFilter, ImageOps


def normalize(image: Image.Image, aspect_ratio: str) -> Image.Image:
    dimensions={'4:5':(1080,1350),'9:16':(1080,1920)}
    if aspect_ratio not in dimensions:raise ValueError('unsupported_production_aspect_ratio')
    target=dimensions[aspect_ratio]
    source=image.convert('RGB')
    if source.size==target:return source
    # The contained foreground is never cropped. A softened copy fills only
    # any surrounding margin, so all generated words, products and faces remain.
    foreground=ImageOps.contain(source,target,method=Image.Resampling.LANCZOS)
    background=ImageOps.fit(source,target,method=Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(radius=16))
    background.paste(foreground,((target[0]-foreground.width)//2,(target[1]-foreground.height)//2))
    return background
