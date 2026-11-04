"""Art direction for lettering generated inside the original image."""
from __future__ import annotations

import json
import re

VERSION = 1
FIELDS = ('personality','letterforms','hierarchy','placement','finish','palette')
STYLES = {
    'editorial': {
        'personality':'Confident, original field-editorial lettering with a practical, energetic character.',
        'letterforms':'Custom compact slab-serif display letters with angled terminals and open counters; a quieter humanist sans for supporting copy.',
        'hierarchy':'An unmistakable display headline, one expressive keyword, and restrained supporting copy; use intentional line breaks and contrasting weights.',
        'placement':'An asymmetric lockup that follows the scene\u2019s movement and uses natural contrast without covering the action.',
        'finish':'Crisp printed-ink character with a subtle tactile edge; keep every letter clean and readable.',
        'palette':'Choose warm ivory or charcoal as the primary lettering color, plus one warm-amber keyword accent; at most two lettering colors.',
    },
    'human': {
        'personality':'Warm, witty, human lettering with a little hand-drawn personality.',
        'letterforms':'Original rounded hand-lettered display forms with lively but controlled rhythm; simple upright supporting letters.',
        'hierarchy':'Give the punchline or meaningful word a clear visual emphasis; keep supporting text calm and smaller.',
        'placement':'Let the type rhythm echo the people\u2019s gesture or exchanged look; keep faces and hands unobscured.',
        'finish':'A clean screen-printed or marker-ink feel, with subtle individual letter character and no messy distress.',
        'palette':'Naturally contrasting cream or dark ink, plus one warm amber accent chosen for the scene.',
    },
    'cinematic': {
        'personality':'Distinctive cinematic title lettering with weight, tension and restraint.',
        'letterforms':'Original sculpted display letters with deliberate cuts and strong proportions; avoid copying a movie, game or superhero logo.',
        'hierarchy':'One bold title gesture and a quieter secondary line; vary scale only to reinforce the actual meaning.',
        'placement':'Compose the title with the scene\u2019s perspective and light, preserving a clear silhouette at phone size.',
        'finish':'A restrained material or ink finish suggested by the scene, with believable depth only when it helps the idea.',
        'palette':'Warm ivory or deep charcoal as the primary lettering color, with a single amber accent; at most two lettering colors.',
    },
    'comic': {
        'personality':'Original, energetic comic lettering with authored character rather than a stock caption font.',
        'letterforms':'Expressive brush-ink display forms with a controlled forward lean; clean consistent dialogue lettering.',
        'hierarchy':'Separate title, dialogue and payoff clearly, and keep the same lettering system through every panel.',
        'placement':'Follow panel reading order and the direction of action; preserve speech clarity and avoid faces, hands and connections.',
        'finish':'Crisp inked contours and a small amount of tactile character; no illegible distortion or decorative effects on every letter.',
        'palette':'Cream and deep ink with one amber accent only when contrast permits; no rainbow typography.',
    },
    'news': {
        'personality':'Sharp, distinctive editorial typography with curiosity and authority.',
        'letterforms':'Expressive high-contrast display serif with strong readable shapes, paired with a quiet humanist sans.',
        'hierarchy':'A decisive headline and precise secondary information; use scale, weight and spacing with a clear first read.',
        'placement':'A disciplined asymmetrical editorial composition aligned with the visible subject and scene geometry.',
        'finish':'Clean ink, considered kerning and purposeful word breaks; original editorial character without news-template clutter.',
        'palette':'Naturally contrasting warm white or charcoal with one restrained amber emphasis.',
    },
}


def resolve(profile=None, *, format_id='', lines=()):
    name='comic' if any(s in format_id for s in ('comic','storypage','story_page','micro_mission','product_intervention')) else 'human' if any(s in format_id for s in ('humor','community','culture')) else 'cinematic' if any(s in format_id for s in ('cinematic','superhero','identity')) else 'news' if 'news' in format_id else 'editorial'
    result={'version':VERSION,'style':name,**STYLES[name]}
    if profile is not None and not isinstance(profile,dict):raise ValueError('typography_design_must_be_an_object')
    for key in FIELDS:
        if profile and key in profile:
            value=profile[key]
            if not isinstance(value,str) or not 8<=len(value.strip())<=800:raise ValueError('invalid_typography_design_'+key)
            result[key]=value.strip()
    text=' '.join(str(line) for line in lines)
    normalized=' '+' '.join(re.findall(r"[\w'-]+",text.casefold()))+' '
    emphasis=(profile or {}).get('emphasis',[])
    if not isinstance(emphasis,list) or any(not isinstance(s,str) for s in emphasis):raise ValueError('typography_emphasis_must_be_words')
    valid=[]
    for phrase in emphasis[:2]:
        words=' '.join(re.findall(r"[\w'-]+",phrase.casefold()))
        if words and len(words)<=40 and ' '+words+' ' in normalized:valid.append(phrase)
    if not valid:
        words=re.findall(r"[\w'-]+",str(next(iter(lines),'')))
        valid=words[-1:]  # A starting direction; the editor still judges whether it earns its emphasis.
    result['emphasis']=valid
    return result


def instruction(design):
    return (
        'LETTERING ART DIRECTION: Treat the words as a designed part of the artwork, with their own personality and visual idea. '
        'Follow this typography brief: '+json.dumps(design,ensure_ascii=False)+'. '
        'Choose deliberate letterforms, hierarchy, kerning, line breaks, word emphasis and a placement that contributes to the scene. '
        'A larger plain white all-caps sans-serif heading is not sufficient design. Color alone is not a type personality. '
        'Use one coherent display voice and one quieter supporting voice; keep the same system across related frames. '
        'One controlled gesture may express a key word through weight, angle, scale or material; do not distort every word. '
        'Use only the exact approved copy. This brief describes design, not extra visible words or labels. '
        'Keep the text readable on a phone, protect the main action, and generate it with the image in the same request. No later text overlay.'
    )
