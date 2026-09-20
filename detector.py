"""Bar/line/letter detection. No GUI. `py detector.py` runs the self-check."""
import os
import string
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# A color spec: hue (OpenCV 0-179) +- h_tol, plus saturation/value ranges. h_tol >= 90 means any hue.
GYM = {
    "box": {"h": 130, "h_tol": 20, "s_min": 60, "s_max": 255, "v_min": 45, "v_max": 215},
    "line": {"h": 0, "h_tol": 90, "s_min": 0, "s_max": 40, "v_min": 230, "v_max": 255},
}
FONTS = ["arialbd.ttf", "arial.ttf", "ariblk.ttf", "segoeuib.ttf", "segoeui.ttf", "seguisb.ttf", "seguibl.ttf",
         "calibrib.ttf", "verdanab.ttf", "tahomabd.ttf", "trebucbd.ttf", "impact.ttf", "bahnschrift.ttf", "consolab.ttf"]
STROKES = (0, 3, 7)  # extra glyph thickness when building shapes: web games love tiny, ultra-heavy letters
SIZE = 32


def spec_from_pixel(bgr, tol=60):
    """Color spec matching pixels similar to one picked BGR pixel."""
    h, s, v = (int(x) for x in cv2.cvtColor(np.uint8([[bgr]]), cv2.COLOR_BGR2HSV)[0, 0])
    return {"h": h, "h_tol": 90 if s < 40 else max(4, tol // 5),
            "s_min": max(0, s - tol), "s_max": min(255, s + tol), "v_min": max(0, v - tol), "v_max": min(255, v + tol)}


def color_mask(hsv, spec):
    """Bool mask of pixels matching the spec. Hue is circular (0-179), so a range may wrap around."""
    s, v = (spec["s_min"], spec["s_max"]), (spec["v_min"], spec["v_max"])
    h0, h1 = (0, 179) if spec["h_tol"] >= 90 else (spec["h"] - spec["h_tol"], spec["h"] + spec["h_tol"])
    m = cv2.inRange(hsv, (max(h0, 0), s[0], v[0]), (min(h1, 179), s[1], v[1]))
    if h0 < 0:
        m |= cv2.inRange(hsv, (180 + h0, s[0], v[0]), (179, s[1], v[1]))
    if h1 > 179:
        m |= cv2.inRange(hsv, (0, s[0], v[0]), (h1 - 180, s[1], v[1]))
    return m > 0


def runs(mask):
    """(start, end) of each contiguous True run in a 1-D bool array."""
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.where(edges == 1)[0], np.where(edges == -1)[0]))


def find_box(hsv, spec):
    """((x0, x1), (y0, y1)) of the highlighted box, or None."""
    mask = color_mask(hsv, spec)
    rows = runs(mask.mean(axis=1) > 0.05)
    if not rows:
        return None
    y0, y1 = max(rows, key=lambda r: r[1] - r[0])
    if y1 - y0 < 8:
        return None
    merged = []
    for c in runs(mask[y0:y1].mean(axis=0) > 0.3):  # letters cover part of a column, so don't require most of it
        if merged and c[0] - merged[-1][1] <= 15:  # rejoin the box where the line crosses it
            merged[-1] = (merged[-1][0], c[1])
        else:
            merged.append(c)
    cols = [c for c in merged if c[1] - c[0] >= 12]
    # widest run = the box; a glowing line alone only makes thin runs
    return (max(cols, key=lambda c: c[1] - c[0]), (y0, y1)) if cols else None


def find_line(hsv, spec, rows):
    """x of the moving line (a column matching `spec` over most of the box height), or None."""
    cols = np.where(color_mask(hsv[rows[0]:rows[1]], spec).mean(axis=0) > 0.75)[0]
    if not len(cols):
        return None
    return int(max(runs(np.isin(np.arange(hsv.shape[1]), cols)), key=lambda r: r[1] - r[0])[0] + 1)


BRIGHT = {"h": 0, "h_tol": 90, "s_min": 0, "s_max": 60, "v_min": 195, "v_max": 255}


def find_marks(bgr):
    """For bars without a coloured target (e.g. a grey translucent skill bar): the moving line is the tallest
    thin bright blob, the letter is the other bright blob(s) next to each other near the middle.
    -> (line_x, (x0, x1, y0, y1) of the letter, letter mask cropped to that box), None where not found."""
    h_img = bgr.shape[0]
    white = color_mask(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), BRIGHT).astype(np.uint8)
    n, _, st, cen = cv2.connectedComponentsWithStats(white)
    labels = _
    blobs = [i for i in range(1, n) if st[i, 4] >= 6 and st[i, 3] >= max(4, h_img * 0.1)
             and abs(cen[i][1] - h_img / 2) <= h_img * 0.3]
    line = None
    thin = [i for i in blobs if st[i, 3] >= 2.5 * st[i, 2]]
    if thin:
        cand = max(thin, key=lambda i: st[i, 3])
        # only blobs about as narrow as the line can be mistaken for it. Bright world behind a see-through bar
        # (sky, a wall, a light) makes wide blobs, and those used to veto the line -> no press while you look around.
        others = [st[i, 3] for i in blobs if i != cand and st[i, 2] <= 3 * st[cand, 2]]
        if not others or st[cand, 3] >= 1.3 * max(others):  # a thin letter (I, 1) is shorter than the line
            line = cand
    rest = [i for i in blobs if i != line]
    line_x = int(cen[line][0]) if line is not None else None
    if line is not None:  # the letter sits at the line's height; bright bits of the world elsewhere don't count
        rest = [i for i in rest if st[line, 1] <= cen[i][1] <= st[line, 1] + st[line, 3]]
    narrow = [i for i in rest if st[i, 2] <= 2.5 * st[i, 3]]  # a letter is never much wider than it is tall
    rest = narrow or rest  # so a bright patch of world behind the bar can't win "biggest blob" and hide the letter
    if not rest:
        return line_x, None, None
    a = max(rest, key=lambda i: st[i, 4])
    ax, ay, aw, ah = st[a, :4]
    group = [i for i in rest if st[i, 0] < ax + aw + ah * 0.6 and st[i, 0] + st[i, 2] > ax - ah * 0.6
             and st[i, 1] < ay + ah and st[i, 1] + st[i, 3] > ay]
    x0, y0 = min(st[i, 0] for i in group), min(st[i, 1] for i in group)
    x1, y1 = max(st[i, 0] + st[i, 2] for i in group), max(st[i, 1] + st[i, 3] for i in group)
    return line_x, (int(x0), int(x1), int(y0), int(y1)), np.isin(labels[y0:y1, x0:x1], group)


def normalize(mask):
    """Crop a bool mask to its content, pad square, resize -> zero-mean unit vector (dot product = correlation).
    No blur: it fills the 2px gaps of small heavy letters, turning E/S/8/X into the same blob."""
    ys, xs = np.where(mask)
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    s = max(crop.shape) + 4
    sq = np.zeros((s, s), np.float32)
    y0, x0 = (s - crop.shape[0]) // 2, (s - crop.shape[1]) // 2
    sq[y0:y0 + crop.shape[0], x0:x0 + crop.shape[1]] = crop
    v = cv2.resize(sq, (SIZE, SIZE), interpolation=cv2.INTER_AREA).ravel()
    v -= v.mean()
    return v / (np.linalg.norm(v) + 1e-6)


def inner(box):
    """(x0, x1, y0, y1) of the area inside the box where the glyph is looked for."""
    (x0, x1), (y0, y1) = box
    pad = max(3, (x1 - x0) // 20)
    return x0 + pad, x1 - pad, y0, y1


def glyph_parts(bw, drop_hairlines=False):
    """Connected blobs of a binary image that can be part of a glyph: not touching the crop edge (the moving
    line, the box border) and, optionally, not 1-2px hairlines (a keycap frame drawn around the letter)."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw.astype(np.uint8))
    h, w = bw.shape
    keep = []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        if area < 8 or x == 0 or y == 0 or x + cw >= w or y + ch >= h:
            continue
        if drop_hairlines and min(cw, ch) <= 2 and max(cw, ch) >= 8:
            continue
        keep.append(i)
    return np.isin(labels, keep) if keep else None


def letter_mask(bgr, box):
    """Pixels of the glyph inside the box (shape of `inner(box)`)."""
    x0, x1, y0, y1 = inner(box)
    crop = bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    # letters are nearly always white text: take near-white pixels first. This skips a coloured keycap
    # frame or glow around the letter, which would otherwise be read as part of the shape.
    white = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (0, 0, 170), (179, 80, 255))
    mask = glyph_parts(white > 0)
    if mask is not None and mask.sum() >= 20:
        return mask
    # otherwise (dark or coloured letter): whichever Otsu class is the minority
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if gray.std() < 12:
        return None
    _, bw = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if bw.mean() > 0.5:
        bw = 1 - bw
    return glyph_parts(bw, drop_hairlines=True)


def render(ch, font_path, size=96, stroke=0):
    im = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(im).text((size // 3, size // 4), ch, font=ImageFont.truetype(font_path, size), fill=255,
                            stroke_width=stroke, stroke_fill=255)
    return np.array(im) > 127


def build_templates(digits=False, fonts=None):
    """(labels, matrix) of glyph vectors from fonts shipped with Windows, in several weights, each also
    squeezed/widened so condensed, wide or extra-bold game fonts still match."""
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    chars = string.ascii_uppercase + (string.digits if digits else "")
    paths = [str(font_dir / f) for f in (fonts or FONTS) if (font_dir / f).exists()]
    labels, vecs = [], []
    for p in paths:
        for c in chars:
            for stroke in STROKES:
                m = render(c, p, stroke=stroke)
                ys, xs = np.where(m)
                crop = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8)
                for sx in (0.75, 1, 1.3):
                    w = max(1, int(crop.shape[1] * sx))
                    vecs.append(normalize(cv2.resize(crop, (w, crop.shape[0]), interpolation=cv2.INTER_NEAREST) > 0))
                    labels.append(c)
    return np.array(labels), np.array(vecs, np.float32)


def rank(mask, templates, n=3):
    """[(char, score), ...] best n distinct characters."""
    labels, matrix = templates
    scores = matrix @ normalize(mask)
    best = {}
    for i in np.argsort(scores)[::-1]:
        best.setdefault(str(labels[i]), round(float(scores[i]), 3))
        if len(best) == n:
            break
    return list(best.items())


def recognize(mask, templates, min_score=0.75):
    """(char, score) best match, char None if nothing is close enough."""
    char, score = rank(mask, templates, 1)[0]
    return (char if score >= min_score else None), score


def press_wait(land, vel, lo, hi, dt, horizon=None):
    """Seconds to wait before sending the key so it lands on the zone center, or None to leave it to a later check.
    land = where the line would be if the key were sent now (px), vel = line speed (px/s, signed), lo..hi = the
    zone, dt = seconds between checks, horizon = how far ahead a press may be booked (default: one and a half
    checks, so a later, better-informed check normally gets the call)."""
    if vel < 0:  # mirror, so the line always travels left to right
        land, lo, hi = -land, -hi, -lo
    wait = ((lo + hi) / 2 - land) / abs(vel)
    if wait > (1.5 * dt if horizon is None else horizon):  # checks come unevenly: leave a spare half check
        return None
    return max(0.0, wait) if land <= hi else None  # past the center but still in the zone: now; past it: too late


if __name__ == "__main__":
    # press timing: zone 100-120 (center 110), 600 px/s, a check every 1/60 s (10 px)
    assert press_wait(85, 600, 100, 120, 1 / 60) is None  # 42 ms away: a later check schedules it
    # a narrow zone the line crosses in less than one check: with a longer horizon it is booked from further out
    assert abs(press_wait(85, 600, 100, 120, 1 / 60, 0.06) - 0.0416666) < 1e-5
    assert abs(press_wait(98, 600, 100, 120, 1 / 60) - 0.02) < 1e-9  # 20 ms: schedule it now
    assert press_wait(115, 600, 100, 120, 1 / 60) == 0.0  # past the center, still in the zone: press now
    assert press_wait(125, 600, 100, 120, 1 / 60) is None  # past the zone
    assert abs(press_wait(118, -600, 100, 120, 1 / 60) - 8 / 600) < 1e-9  # moving left

    # fake gym bar: dark bar, purple box with a white E, white line left of the box
    img = np.full((130, 1020, 3), (30, 25, 25), np.uint8)
    cv2.rectangle(img, (0, 0), (1019, 129), (230, 180, 190), 3)
    cv2.rectangle(img, (410, 5), (670, 125), (98, 52, 68), -1)
    cv2.putText(img, "E", (505, 90), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 7)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    box = find_box(hsv, GYM["box"])
    assert box and abs(box[0][0] - 410) <= 2 and abs(box[0][1] - 671) <= 2, box
    assert find_line(hsv, GYM["line"], box[1]) is None, "letter mistaken for line"
    assert recognize(letter_mask(img, box), build_templates())[0] == "E"
    cv2.line(img, (85, 0), (85, 129), (255, 255, 255), 4)
    assert abs(find_line(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), GYM["line"], box[1]) - 85) <= 3
    # line crossing the box (and a tall letter) must not split it
    crossing = img.copy()
    cv2.putText(crossing, "I", (600, 118), cv2.FONT_HERSHEY_SIMPLEX, 3.8, (255, 255, 255), 8)
    cv2.line(crossing, (470, 0), (470, 129), (255, 255, 255), 6)
    hsv = cv2.cvtColor(crossing, cv2.COLOR_BGR2HSV)
    box2 = find_box(hsv, GYM["box"])
    assert box2 and abs(box2[0][0] - 410) <= 2 and abs(box2[0][1] - 671) <= 2, box2
    assert abs(find_line(hsv, GYM["line"], box2[1]) - 470) <= 3

    # the real "E TIMING" browser game: a 10x16 px ultra-heavy E (pixels copied from a screen capture)
    # inside a dark keycap with a thin light-purple frame, inside a narrow purple zone
    real_e = np.array([[c == "#" for c in row] for row in (
        "##########|##########|##########|##########|#####.....|#####.....|#########.|#########.|"
        "#########.|#########.|#####.....|#####.....|##########|##########|##########|##########").split("|")])
    game = np.full((78, 613, 3), (22, 14, 16), np.uint8)
    cv2.rectangle(game, (355, 9), (460, 72), (110, 45, 70), -1)
    cv2.rectangle(game, (387, 21), (427, 61), (55, 27, 42), -1)
    cv2.rectangle(game, (387, 21), (427, 61), (160, 110, 140), 1)
    game[33:49, 402:412][real_e] = (245, 245, 245)
    cv2.line(game, (440, 5), (440, 74), (255, 255, 255), 3)
    hsv = cv2.cvtColor(game, cv2.COLOR_BGR2HSV)
    box3 = find_box(hsv, GYM["box"])
    assert box3 and abs(box3[0][0] - 355) <= 2 and abs(box3[0][1] - 461) <= 2, box3
    templates = build_templates(digits=True)
    top = rank(letter_mask(game, box3), templates)
    print("keycap game letter:", top)
    assert top[0][0] == "E" and top[0][1] >= 0.75, top

    # plain see-through skill bar: small white letter marks the spot, white pill line, busy world behind it
    world = cv2.resize(np.random.default_rng(3).integers(40, 230, (8, 49, 3)).astype(np.uint8), (385, 57))
    world[12:48, 22:375] = (world[12:48, 22:375] * 0.35 + 25).astype(np.uint8)
    pil = Image.fromarray(world)
    ImageDraw.Draw(pil).text((108, 30), "E", font=ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows"))
                             / "Fonts" / "seguibl.ttf"), 19), fill=(255, 255, 255), anchor="mm")
    skill = np.array(pil)
    cv2.rectangle(skill, (198, 22), (202, 40), (255, 255, 255), -1)
    line_x, letter_box, mask = find_marks(skill)
    top = rank(mask, build_templates(fonts=[f for f in FONTS if f != "seguibl.ttf"]))
    print("skill bar:", line_x, letter_box, top)
    assert abs(line_x - 200) <= 2 and letter_box and 100 <= letter_box[0] <= 108 and top[0][0] == "E", top
    # a bright patch of world behind the see-through bar (looking at the sky, a wall, a light) must not veto the
    # line nor be taken for the letter -- that was every miss while the mouse moved
    busy = skill.copy()
    cv2.rectangle(busy, (250, 14), (340, 46), (255, 255, 255), -1)
    line_x, letter_box, mask = find_marks(busy)
    assert abs(line_x - 200) <= 2, f"bright background hid the line: {line_x}"
    assert letter_box and 100 <= letter_box[0] <= 108, f"bright background taken for the letter: {letter_box}"

    # every letter drawn in one real font must be read using only the *other* fonts, both at a normal
    # size and small + extra heavy (Impact is skipped as held-out: too condensed to resemble anything else)
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    fonts = [f for f in FONTS if (font_dir / f).exists()]
    miss, heavy_miss = [], []
    for held_out in [f for f in fonts if f != "impact.ttf"]:
        templates = build_templates(fonts=[f for f in fonts if f != held_out])
        for c in string.ascii_uppercase:
            if rank(render(c, str(font_dir / held_out), 40), templates, 1)[0][0] != c:
                miss.append(f"{held_out}:{c}")
            if rank(render(c, str(font_dir / held_out), 20, stroke=1), templates, 1)[0][0] != c:
                heavy_miss.append(f"{held_out}:{c}")
    total = (len(fonts) - 1) * 26
    print(f"unseen-font accuracy {total - len(miss)}/{total}, small heavy {total - len(heavy_miss)}/{total}", miss)
    assert len(miss) <= total * 0.02, "letter recognition too weak"
    assert len(heavy_miss) <= total * 0.12, "small heavy letter recognition too weak"
    print("ok")
