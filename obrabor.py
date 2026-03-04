import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from google import genai
from google.genai import types
from PIL import Image
import io

os.environ["GOOGLE_API_KEY"] = "REDACTED"

client = genai.Client()

prompt = """
Создать сверхреалистичное студийное фото клубники в шоколаде, сохранив натуральные детали и текстуру.
Освещение — профессиональный студийный сетап с мягким рассеянным светом и естественными реалистичными тенями.
Фон белый, но с лёгкой глубиной, без плоского эффекта.
Добавить мягкую падающую тень под клубникой.
Шоколад — глянцевый, с натуральными бликами, без искусственного блеска. Постарайся сделать шоколад чуть более блестящим(будто бы он почти застыл, но при этом не был жидким), но не меняй его цвет.
Клубника яркая, сочная, максимально детализированная, если на ней есть шоколад, то она должна быть полностью им покрыта.
Удалить шум и мусор, выровнять поверхность, сохранив реалистичность.
Кадр — крупный план, предмет в центре.
Ракурс и композицию НЕ менять.
"""


input_path = "M8.PNG"   #сюда название фотки
output_prefix = "edited_M8" #сюда префикс вывода

with open(input_path, "rb") as f:
    img_bytes = f.read()

with Image.open(input_path) as im:
    mime_type = Image.MIME[im.format]

image_part = types.Part.from_bytes(
    data=img_bytes,
    mime_type=mime_type
)


def generate_variant(idx: int) -> str | None:
    response = client.models.generate_content(
        model="gemini-3.1-flash-image-preview",
        contents=[prompt, image_part],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio="1:1",
                image_size="4K",
            ),
        ),
    )

    for part in response.parts:
        if part.inline_data is not None:
            img = Image.open(io.BytesIO(part.inline_data.data))
            out_name = f"{output_prefix}_{idx}.png"
            img.save(out_name)
            return out_name
    return None


n_variants = 5

with ThreadPoolExecutor(max_workers=n_variants) as executor:
    futures = [
        executor.submit(generate_variant, i + 1)
        for i in range(n_variants)
    ]

    for future in as_completed(futures):
        result = future.result()
        if result:
            print("Saved:", result)