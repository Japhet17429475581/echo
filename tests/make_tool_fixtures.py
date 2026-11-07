"""Deterministic, original fixtures; invoked with /opt/tools/bin/python."""

import subprocess
import sys
from pathlib import Path

import zxingcpp
from PIL import Image, ImageDraw, ImageFont

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 44)
image = Image.new("RGB", (1000, 320), "white")
draw = ImageDraw.Draw(image)
draw.text((35, 45), "ENGLISH ECHO 12345", font=font, fill="black")
draw.text((35, 150), "ПРОВЕРКА ЭХО 67890", font=font, fill="black")
image.save(root / "ocr.png")
original = Image.new("RGB", (320, 180), "#acf")
original.save(root / "metadata.jpg")
subprocess.run(
    [
        "exiftool",
        "-overwrite_original",
        "-Make=ECHOFixture",
        "-Model=TestDevice",
        "-DateTimeOriginal=2025:06:01 12:30:00",
        "-OffsetTimeOriginal=+03:00",
        "-Orientation#=6",
        "-GPSLatitude=55",
        "-GPSLatitudeRef=N",
        "-GPSLongitude=37",
        "-GPSLongitudeRef=E",
        str(root / "metadata.jpg"),
    ],
    check=True,
    capture_output=True,
)
canvas = Image.new("RGB", (1100, 480), "white")
payloads = [
    "http://fixture.test/article",
    "WIFI:T:WPA;S:ECHO-FIXTURE;P:not-a-real-password;;",
    "javascript:alert(1)",
]
for index, text in enumerate(payloads):
    code = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    tile = Image.fromarray(code.to_image(scale=5)).convert("RGB")
    canvas.paste(tile, (20 + index * 350, 30))
canvas.save(root / "qr.png")
Image.new("RGB", (400, 400), "white").save(root / "blank.png")
Image.new("RGB", (4000, 2001), "white").save(root / "oversize.png")
(root / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\nBROKEN")
(root / "own-notes.txt").write_text("Own ECHO workspace fixture.\n", "utf-8")
