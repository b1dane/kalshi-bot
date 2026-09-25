"""Generate PNG app icons (192 and 512) with PIL. Run with python3.11."""
from PIL import Image, ImageDraw

def make_icon(size, path):
    img = Image.new("RGB", (size, size), "#12224a")
    d = ImageDraw.Draw(img)
    # gradient-ish background via vertical bands
    import random
    # draw rounded dark base
    # We'll just overlay darker toward bottom
    for y in range(size):
        t = y / size
        band = int(10 + t * 6)
        r, g, b = 18 - band, 26 - band, 48 - band
        d.line([(0, y), (size, y)], fill=(max(r,0), max(g,0), max(b,0)))

    s = size / 512.0
    # sparkline (green)
    points = [(64*s,340*s),(176*s,300*s),(260*s,230*s),(330*s,250*s),(448*s,130*s)]
    line_w = max(6, int(28*s))
    d.line(points, fill=(52,211,153), width=line_w, joint="curve")
    # dot
    dot_r = max(8, int(26*s))
    d.ellipse([448*s-dot_r,130*s-dot_r,448*s+dot_r,130*s+dot_r], fill=(52,211,153))
    # candlestick 1 (green)
    x = 180*s; y=330*s; w=34*s; h=90*s
    d.rectangle([x,y,x+w,y+h], fill=(52,211,153))
    lx=197*s; line_w2=max(4,int(16*s))
    d.line([lx,300*s,lx,330*s], fill=(52,211,153), width=line_w2)
    # candlestick 2 (red)
    x=280*s; y=280*s; w=34*s; h=140*s
    d.rectangle([x,y,x+w,y+h], fill=(248,113,113))
    lx=297*s
    d.line([lx,250*s,lx,280*s], fill=(248,113,113), width=line_w2)

    img.save(path)
    print("saved", path, img.size)

make_icon(192, "icon-192.png")
make_icon(512, "icon-512.png")
