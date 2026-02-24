import os
import io
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from google import genai
from google.genai import types
from PIL import Image
import logging
from pillow_heif import register_heif_opener


class GoogleServerOverloadedError(Exception):
    """Ошибка перегрузки серверов Google (503)"""
    pass

# Регистрируем поддержку HEIC/HEIF
register_heif_opener()

from config import (
    GOOGLE_API_KEY,
    PROCESSING_PROMPT,
    GEMINI_MODEL,
    IMAGE_ASPECT_RATIO,
    IMAGE_SIZE,
    MAX_WORKERS
)

logger = logging.getLogger(__name__)

# Настройка Google API
if GOOGLE_API_KEY:
    os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY

client = genai.Client()


def generate_variant(image_part: types.Part, idx: int, prompt: str = None, aspect_ratio: str = "1:1") -> bytes | None:
    """
    Генерация одного варианта изображения (точно как в obrabor.py)

    Args:
        image_part: Часть изображения для Gemini API
        idx: Индекс варианта
        prompt: Промпт для обработки (если None, используется стандартный)
        aspect_ratio: Соотношение сторон (1:1, 3:4, 16:9, 9:16)

    Returns:
        bytes: Байты обработанного изображения или None в случае ошибки
    """
    try:
        # Используем кастомный промпт или стандартный
        used_prompt = prompt if prompt is not None else PROCESSING_PROMPT

        # Используем точно такой же вызов, как в obrabor.py
        response = client.models.generate_content(
            model="gemini-3-pro-image-preview",
            contents=[used_prompt, image_part],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio=aspect_ratio,
                    image_size="4K",
                ),
            ),
        )

        # Получаем данные и пересохраняем через PIL (как в obrabor.py)
        for part in response.parts:
            if part.inline_data is not None:
                # Открываем и пересохраняем как PNG (как в obrabor.py)
                img = Image.open(io.BytesIO(part.inline_data.data))
                output = io.BytesIO()
                img.save(output, format='PNG')
                png_bytes = output.getvalue()
                logger.info(f"Вариант {idx} успешно сгенерирован ({len(png_bytes) / 1024 / 1024:.1f} МБ)")
                return png_bytes

        logger.error(f"Вариант {idx}: Gemini вернул 200 OK, но БЕЗ изображения (отказ генерации)")
        return None

    except Exception as e:
        error_str = str(e)
        # Проверяем на ошибку 503 (сервер перегружен)
        if "503" in error_str or "Service Unavailable" in error_str or "overloaded" in error_str.lower():
            logger.error(f"Серверы Google перегружены (503): {e}")
            raise GoogleServerOverloadedError("Серверы Google временно перегружены. Попробуйте позже.")
        logger.error(f"Ошибка при генерации варианта {idx}: {e}")
        return None


def generate_variant_with_reference(main_part: types.Part, ref_part: types.Part, idx: int, prompt: str = None, aspect_ratio: str = "1:1") -> bytes | None:
    """Генерация варианта с референсом"""
    try:
        used_prompt = prompt if prompt else PROCESSING_PROMPT
        full_prompt = (
            "ВАЖНО: Первое изображение - РЕФЕРЕНС (образец стиля/цветов/освещения). "
            "Второе - ОСНОВНОЕ для обработки с учётом референса.\n\n" + used_prompt
        )
        response = client.models.generate_content(
            model="gemini-3-pro-image-preview",
            contents=[full_prompt, ref_part, main_part],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect_ratio, image_size="4K"),
            ),
        )
        for part in response.parts:
            if part.inline_data is not None:
                img = Image.open(io.BytesIO(part.inline_data.data))
                output = io.BytesIO()
                img.save(output, format='PNG')
                png_bytes = output.getvalue()
                logger.info(f"Вариант {idx} с референсом сгенерирован ({len(png_bytes) / 1024 / 1024:.1f} МБ)")
                return png_bytes
        logger.error(f"Вариант {idx} с референсом: Gemini вернул 200 OK, но БЕЗ изображения (отказ генерации)")
        return None
    except Exception as e:
        error_str = str(e)
        # Проверяем на ошибку 503 (сервер перегружен)
        if "503" in error_str or "Service Unavailable" in error_str or "overloaded" in error_str.lower():
            logger.error(f"Серверы Google перегружены (503): {e}")
            raise GoogleServerOverloadedError("Серверы Google временно перегружены. Попробуйте позже.")
        logger.error(f"Ошибка варианта {idx} с референсом: {e}")
        return None


def prepare_image(image_bytes) -> tuple[bytes, str, types.Part]:
    """
    Подготовка изображения для обработки

    Returns:
        tuple: (img_bytes, mime_type, image_part)
    """
    # Читаем байты
    if isinstance(image_bytes, io.BytesIO):
        img_bytes = image_bytes.read()
        image_bytes.seek(0)
    else:
        img_bytes = image_bytes

    # Определяем MIME-тип и конвертируем HEIC в PNG
    try:
        with Image.open(io.BytesIO(img_bytes)) as im:
            # Если формат HEIC/HEIF, конвертируем в PNG
            if im.format in ('HEIC', 'HEIF'):
                logger.info("Обнаружен HEIC/HEIF формат, конвертируем в PNG")
                output = io.BytesIO()
                im.save(output, format='PNG')
                img_bytes = output.getvalue()
                mime_type = "image/png"
            else:
                mime_type = Image.MIME.get(im.format, "image/png")
    except Exception as e:
        logger.error(f"Ошибка при определении формата изображения: {e}")
        mime_type = "image/png"

    # Создаем часть изображения для API
    image_part = types.Part.from_bytes(
        data=img_bytes,
        mime_type=mime_type
    )

    return img_bytes, mime_type, image_part


async def process_image_stream(image_bytes: io.BytesIO, variants_count: int = 5, custom_prompt: str = None, aspect_ratio: str = "1:1"):
    """
    Асинхронный генератор - отдает результаты по мере готовности

    Args:
        image_bytes: Байты исходного изображения
        variants_count: Количество вариантов для генерации
        custom_prompt: Кастомный промпт (если None, используется стандартный)
        aspect_ratio: Соотношение сторон (1:1, 3:4, 16:9, 9:16)

    Yields:
        tuple: (idx, bytes) - номер варианта и байты изображения
    """
    try:
        _, _, image_part = prepare_image(image_bytes)

        prompt_type = "кастомный" if custom_prompt else "стандартный"
        logger.info(f"Начинаем генерацию {variants_count} вариантов с {prompt_type} промптом, соотношение сторон: {aspect_ratio}")

        loop = asyncio.get_event_loop()

        with ThreadPoolExecutor(max_workers=min(variants_count, MAX_WORKERS)) as executor:
            # Создаем задачи с индексами
            tasks = {}
            for i in range(variants_count):
                task = asyncio.ensure_future(
                    loop.run_in_executor(
                        executor,
                        generate_variant,
                        image_part,
                        i + 1,
                        custom_prompt,
                        aspect_ratio
                    )
                )
                tasks[task] = i + 1

            # Отдаем результаты по мере готовности
            pending = set(tasks.keys())
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    idx = tasks[task]
                    try:
                        result = task.result()
                        if result:
                            yield (idx, result)
                    except GoogleServerOverloadedError:
                        raise  # Пробрасываем 503 ошибку наверх в bot.py
                    except Exception as e:
                        logger.error(f"Ошибка при получении результата варианта {idx}: {e}")

    except GoogleServerOverloadedError:
        raise  # Пробрасываем 503 ошибку наверх в bot.py
    except Exception as e:
        logger.error(f"Критическая ошибка при обработке изображения: {e}")


async def process_image_with_reference_stream(main_bytes: bytes, ref_bytes: bytes, variants_count: int = 5, custom_prompt: str = None, aspect_ratio: str = "1:1"):
    """Асинхронный генератор для обработки с референсом"""
    try:
        _, _, main_part = prepare_image(main_bytes)
        _, _, ref_part = prepare_image(ref_bytes)
        logger.info(f"Генерация {variants_count} вариантов С РЕФЕРЕНСОМ")
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=min(variants_count, MAX_WORKERS)) as executor:
            tasks = {}
            for i in range(variants_count):
                task = asyncio.ensure_future(loop.run_in_executor(executor, generate_variant_with_reference, main_part, ref_part, i + 1, custom_prompt, aspect_ratio))
                tasks[task] = i + 1
            pending = set(tasks.keys())
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    idx = tasks[task]
                    try:
                        result = task.result()
                        if result:
                            yield (idx, result)
                    except GoogleServerOverloadedError:
                        raise  # Пробрасываем 503 ошибку наверх в bot.py
                    except Exception as e:
                        logger.error(f"Ошибка варианта {idx}: {e}")
    except GoogleServerOverloadedError:
        raise  # Пробрасываем 503 ошибку наверх в bot.py
    except Exception as e:
        logger.error(f"Критическая ошибка с референсом: {e}")


async def process_image(image_bytes: io.BytesIO, variants_count: int = 5, custom_prompt: str = None, aspect_ratio: str = "1:1") -> list[bytes]:
    """
    Асинхронная обработка изображения с генерацией нескольких вариантов
    (для обратной совместимости)

    Args:
        image_bytes: Байты исходного изображения
        variants_count: Количество вариантов для генерации
        custom_prompt: Кастомный промпт (если None, используется стандартный)
        aspect_ratio: Соотношение сторон (1:1, 3:4, 16:9, 9:16)

    Returns:
        list[bytes]: Список байтов обработанных изображений
    """
    results = []
    async for idx, data in process_image_stream(image_bytes, variants_count, custom_prompt, aspect_ratio):
        results.append(data)
    logger.info(f"Успешно сгенерировано {len(results)} из {variants_count} вариантов")
    return results


def process_image_sync(image_path: str, output_prefix: str, variants_count: int = 5, custom_prompt: str = None) -> list[str]:
    """
    Синхронная обработка изображения (для совместимости с obrabor.py)

    Args:
        image_path: Путь к исходному изображению
        output_prefix: Префикс для выходных файлов
        variants_count: Количество вариантов
        custom_prompt: Кастомный промпт (если None, используется стандартный)

    Returns:
        list[str]: Список путей к сохраненным файлам
    """
    try:
        # Читаем файл
        with open(image_path, "rb") as f:
            img_bytes = f.read()

        # Определяем MIME-тип
        with Image.open(image_path) as im:
            mime_type = Image.MIME[im.format]

        image_part = types.Part.from_bytes(
            data=img_bytes,
            mime_type=mime_type
        )

        saved_files = []

        with ThreadPoolExecutor(max_workers=min(variants_count, MAX_WORKERS)) as executor:
            futures = {
                executor.submit(generate_variant, image_part, i + 1, custom_prompt): i + 1
                for i in range(variants_count)
            }

            for future in as_completed(futures):
                idx = futures[future]
                result = future.result()

                if result:
                    # Сохраняем изображение
                    img = Image.open(io.BytesIO(result))
                    out_name = f"{output_prefix}_{idx}.png"
                    img.save(out_name)
                    saved_files.append(out_name)
                    print(f"Saved: {out_name}")

        return saved_files

    except Exception as e:
        logger.error(f"Ошибка при синхронной обработке: {e}")
        return []
