#!/usr/bin/env python3
"""Export one finished collage as nine seamless tiles and a delivery bundle."""

import argparse
import io
import json
from pathlib import Path
import tempfile
import zipfile

from PIL import Image, ImageCms, ImageColor, ImageOps


def prepare_image(source, fit, tile_size, background):
    with Image.open(source) as original:
        if getattr(original, "n_frames", 1) != 1:
            raise ValueError("请先选择单张静态图片，不能直接处理多帧文件。")
        raw_size = original.size
        oriented = ImageOps.exif_transpose(original)
        oriented_size = oriented.size
        alpha = oriented.convert("RGBA").getchannel("A")
        profile = original.info.get("icc_profile")
        if profile:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            color_input = oriented if oriented.mode in ("RGB", "CMYK", "LAB", "L") else oriented.convert("RGB")
            rgb = ImageCms.profileToProfile(
                color_input, src_profile, ImageCms.createProfile("sRGB"), outputMode="RGB"
            )
        else:
            rgb = oriented.convert("RGB")
        canvas = Image.new("RGB", rgb.size, background)
        canvas.paste(rgb, (0, 0), alpha)

    width, height = canvas.size
    fit_box = None
    if width != height:
        if fit == "strict":
            raise ValueError("输入不是正方形。请修正构图，或明确指定 --fit crop / --fit pad。")
        side = min(width, height) if fit == "crop" else max(width, height)
        if fit == "crop":
            left, top = (width - side) // 2, (height - side) // 2
            fit_box = [left, top, left + side, top + side]
            canvas = canvas.crop(fit_box)
        else:
            padded = Image.new("RGB", (side, side), background)
            offset = ((side - width) // 2, (side - height) // 2)
            padded.paste(canvas, offset)
            fit_box = [offset[0], offset[1], offset[0] + width, offset[1] + height]
            canvas = padded

    before_resize = canvas.size
    if tile_size is not None:
        if tile_size <= 0 or tile_size > 8192:
            raise ValueError("--tile-size 必须为 1–8192 的整数。")
        side = tile_size * 3
        if canvas.size != (side, side):
            canvas = canvas.resize((side, side), Image.Resampling.LANCZOS)
    elif canvas.width < 3 or canvas.width % 3:
        raise ValueError("正方形边长必须至少为 3 且能被 3 整除，或指定 --tile-size。")

    metadata = {
        "input_name": Path(source).name,
        "input_size": list(raw_size),
        "oriented_size": list(oriented_size),
        "fit": fit,
        "fit_box": fit_box,
        "size_before_resize": list(before_resize),
        "output_size": list(canvas.size),
        "resized": canvas.size != before_resize,
        "upscaled": canvas.width > before_resize[0],
        "color": "sRGB (converted from embedded ICC)" if profile else "sRGB (assumed; no embedded ICC)",
        "background": list(background),
    }
    # Fresh pixels prevent copying EXIF or other private source metadata.
    clean = Image.new("RGB", canvas.size)
    clean.paste(canvas)
    return clean, metadata


def export_grid(source, output, fit="strict", tile_size=None, background="#ffffff"):
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("输出目录必须不存在或为空，已有文件不会被覆盖。")
    color = ImageColor.getrgb(background)
    if len(color) != 3:
        raise ValueError("背景色必须是不透明的 RGB 颜色。")
    full, metadata = prepare_image(source, fit, tile_size, color)
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    side = full.width // 3
    metadata.update({"tile_size": [side, side], "order": "row-major: left-to-right, top-to-bottom", "tiles": []})
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build in a sibling directory so an encoding failure leaves no partial delivery.
    with tempfile.TemporaryDirectory(prefix=".nine-grid-", dir=output.parent) as temporary:
        staging = Path(temporary) / "delivery"
        staging.mkdir()
        full.save(staging / "full.png", icc_profile=icc)
        preview_side = min(side, 360)
        gap = 10
        preview = Image.new("RGB", (preview_side * 3 + gap * 2,) * 2, (235, 235, 235))
        for row in range(3):
            for column in range(3):
                number = row * 3 + column + 1
                name = f"{number:02d}.png"
                box = (column * side, row * side, (column + 1) * side, (row + 1) * side)
                tile = full.crop(box)
                tile.save(staging / name, icc_profile=icc)
                thumb = tile.resize((preview_side, preview_side), Image.Resampling.LANCZOS)
                preview.paste(thumb, (column * (preview_side + gap), row * (preview_side + gap)))
                metadata["tiles"].append({"file": name, "row": row + 1, "column": column + 1, "box": list(box)})
        preview.save(staging / "preview.jpg", quality=92, icc_profile=icc)
        (staging / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (staging / "posting-order.txt").write_text(
            "微信发布时依次选择 01.png 至 09.png：\n\n01 02 03\n04 05 06\n07 08 09\n\n"
            "full.png 为完整作品。preview.jpg 带间隔，仅用于预览。\n"
            "切片按完整图直接裁切，无重叠、无额外边框。\n", encoding="utf-8"
        )
        files = sorted(staging.iterdir())
        with zipfile.ZipFile(staging / "nine-grid.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                archive.write(file, file.name)
        if output.exists():
            output.rmdir()  # Fails safely if it became nonempty during processing.
        staging.rename(output)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="已完成的拼贴母图")
    parser.add_argument("--output", required=True, type=Path, help="新的或空的输出目录")
    parser.add_argument("--fit", choices=("strict", "crop", "pad"), default="strict")
    parser.add_argument("--tile-size", type=int, help="明确指定每片边长，会缩放母图")
    parser.add_argument("--background", default="#ffffff", help="透明区域或补边的背景色")
    args = parser.parse_args()
    try:
        metadata = export_grid(args.input, args.output, args.fit, args.tile_size, args.background)
    except (ValueError, OSError, ImageCms.PyCMSError) as error:
        parser.exit(2, f"错误：{error}\n")
    print(json.dumps({"output": str(args.output.resolve()), "size": metadata["output_size"],
                      "resized": metadata["resized"], "upscaled": metadata["upscaled"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
