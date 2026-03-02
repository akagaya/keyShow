"""KeyShow.ico を生成するスクリプト。"""
from PIL import Image, ImageDraw, ImageFont

SIZES = [16, 32, 48, 64, 128, 256]


def create_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = max(1, size // 16)
    radius = max(2, size // 6)
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=radius,
        fill=(30, 30, 30, 255),
    )
    try:
        font = ImageFont.truetype("segoeui.ttf", int(size * 0.45))
    except OSError:
        font = ImageFont.load_default()
    draw.text(
        (size / 2, size / 2), "kS",
        fill=(220, 220, 220, 255), font=font, anchor="mm",
    )
    return img


def main() -> None:
    images = [create_icon(s) for s in SIZES]
    # 最大サイズを基準に、append_images で各サイズを追加
    images[-1].save(
        "KeyShow.ico",
        format="ICO",
        append_images=images[:-1],
    )
    print(f"KeyShow.ico created ({len(SIZES)} sizes: {SIZES})")


if __name__ == "__main__":
    main()
