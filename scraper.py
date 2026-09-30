#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Envato Elements Stock Video Scraper
- Завантажує відео з Envato Elements без водяних знаків та без підписки у багатопотоковому режимі.
- Підтримує вибір співвідношення сторін / орієнтації: 16:9 (horizontal) або 9:16 (vertical).
- Створює наочну контактну сітку кадрів (preview sheet) для швидкого перегляду змісту відео.
- Працює у фоновому режимі з детальним журналом дій.
"""

import sys
import io

# Захист від помилок кодування cp1252/cp1251 у консолі Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
import re
import math
import json
import base64
import time
import logging
import argparse
import threading
from urllib.parse import quote_plus, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests

try:
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    HAS_CV2_PIL = True
except ImportError:
    HAS_CV2_PIL = False

BASE_DOMAIN = "https://elements.envato.com"
HYDRATION_MARKER = 'INITIAL_HYDRATION_DATA="'

thread_local = threading.local()

def get_thread_session() -> requests.Session:
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session(impersonate="chrome")
    return thread_local.session

def setup_logger(log_file: str, verbose: bool = False):
    logger = logging.getLogger("EnvatoScraper")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger

def sanitize_filename(name: str, max_length: int = 120) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip('. ')
    if not cleaned:
        cleaned = "video"
    return cleaned[:max_length]

def extract_hydration_data(html: str) -> dict:
    idx = html.find(HYDRATION_MARKER)
    if idx == -1:
        return {}
    start = idx + len(HYDRATION_MARKER)
    end = html.find('"', start)
    if end == -1:
        return {}
    b64_str = html[start:end]
    b64_str += '=' * ((4 - len(b64_str) % 4) % 4)
    try:
        decoded_bytes = base64.b64decode(b64_str)
        return json.loads(decoded_bytes.decode('utf-8'))
    except Exception:
        return {}

def normalize_aspect_ratio_param(aspect_ratio: str | None) -> str:
    if not aspect_ratio:
        return "all"
    val = aspect_ratio.strip().lower()
    if val in ["16:9", "16/9", "16x9", "16-9", "horizontal", "h"]:
        return "horizontal"
    if val in ["9:16", "9/16", "9x16", "9-16", "vertical", "v"]:
        return "vertical"
    return "all"

def resolve_search_url(url_or_query: str, orientation: str = "all") -> str:
    if url_or_query.startswith("http://") or url_or_query.startswith("https://"):
        base_url = re.sub(r'/pg-\d+', '', url_or_query).rstrip('/')
    else:
        encoded_query = quote_plus(url_or_query.strip())
        base_url = f"{BASE_DOMAIN}/stock-video/{encoded_query}"

    if orientation == "horizontal" and "orientation-horizontal" not in base_url and "orientation-vertical" not in base_url:
        base_url = f"{base_url}/orientation-horizontal"
    elif orientation == "vertical" and "orientation-vertical" not in base_url and "orientation-horizontal" not in base_url:
        base_url = f"{base_url}/orientation-vertical"

    return base_url

def build_page_url(base_url: str, page_num: int) -> str:
    clean_url = re.sub(r'/pg-\d+', '', base_url).rstrip('/')
    if page_num <= 1:
        return clean_url
    return f"{clean_url}/pg-{page_num}"

def is_matching_aspect_ratio(item: dict, orientation: str) -> bool:
    if orientation == "all":
        return True
    ar = item.get("aspectRatio")
    if ar is None:
        return True
    try:
        val = float(ar)
        if orientation == "horizontal":
            return val >= 1.0
        elif orientation == "vertical":
            return val < 1.0
    except (ValueError, TypeError):
        return True
    return True

def fetch_page_items(session: requests.Session, page_url: str, logger: logging.Logger):
    logger.info(f"Запит сторінки пошуку: {page_url}")
    try:
        resp = session.get(page_url, timeout=25)
        if resp.status_code != 200:
            logger.warning(f"Статус відповіді {resp.status_code} для {page_url}")
            return [], {}
        data = extract_hydration_data(resp.text)
        page_data = data.get("page", {}).get("data", {}).get("data", {})
        items = page_data.get("items", [])
        pagination = page_data.get("pagination", {})
        return items, pagination
    except Exception as e:
        logger.error(f"Помилка при запиті {page_url}: {e}")
        return [], {}

def fetch_unwatermarked_url(session: requests.Session, item: dict, logger: logging.Logger) -> str | None:
    item_url = item.get("itemUrl")
    if not item_url:
        return None

    full_item_url = urljoin(BASE_DOMAIN, item_url)
    try:
        resp = session.get(full_item_url, timeout=20)
        if resp.status_code == 200:
            data = extract_hydration_data(resp.text)
            item_detail = data.get("page", {}).get("data", {}).get("data", {}).get("item", {})
            video_info = item_detail.get("video", {})
            unwatermarked = video_info.get("unwatermarkedVideos", {})
            if unwatermarked:
                candidate = unwatermarked.get("highBitrate") or unwatermarked.get("lowBitrate")
                if candidate:
                    return candidate
    except Exception as e:
        logger.debug(f"Не вдалося отримати unwatermarked зі сторінки товару {item.get('id')}: {e}")

    # Fallback: перевірка чи прев'ю на пошуковій картці вже без водяного знаку
    standard_video = item.get("video", {}).get("standard")
    if standard_video and "watermarked" not in standard_video:
        return standard_video

    return None

def generate_contact_sheet(video_path: str, output_sheet_path: str, step_sec_override: int | None, logger: logging.Logger) -> bool:
    if not HAS_CV2_PIL:
        logger.debug("cv2 або PIL не встановлені, генерацію контактної сітки пропущено.")
        return False

    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.warning(f"Не вдалося відкрити відео для генерації сітки: {video_path}")
            return False

        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = frame_count / fps if fps > 0 else 0

        if duration <= 0:
            cap.release()
            return False

        if step_sec_override and step_sec_override > 0:
            step_sec = step_sec_override
        else:
            # Алгоритм за замовчуванням:
            # якщо відео 9с -> крок 1с (9 кадрів)
            # якщо 18с -> крок 2с (9 кадрів)
            # тобто крок = max(1, round(duration / 9))
            step_sec = max(1, math.ceil(duration / 9.0))

        timestamps = list(range(0, int(duration) + 1, step_sec))
        if not timestamps:
            timestamps = [0]

        frames = []
        for ts in timestamps:
            frame_idx = int(ts * fps)
            if frame_idx >= frame_count:
                frame_idx = max(0, frame_count - 1)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if ret:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append((ts, frame_rgb))

        cap.release()

        if not frames:
            return False

        # Формування сітки: 3 або 4 колонки
        cols = 3 if len(frames) <= 9 else 4
        rows = math.ceil(len(frames) / cols)

        sample_h, sample_w = frames[0][1].shape[:2]
        is_vertical = sample_h > sample_w

        if is_vertical:
            thumb_w, thumb_h = 180, 320
        else:
            thumb_w, thumb_h = 320, 180

        padding = 6
        grid_w = cols * thumb_w + (cols + 1) * padding
        grid_h = rows * thumb_h + (rows + 1) * padding + 35 # заголовок зверху

        sheet = Image.new("RGB", (grid_w, grid_h), (25, 25, 30))
        draw = ImageDraw.Draw(sheet)

        video_name = os.path.basename(video_path)
        info_header = f"{video_name} | {duration:.1f}s | крок {step_sec}s | кадрів: {len(frames)}"
        font_h = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 13) if os.path.exists('C:/Windows/Fonts/arial.ttf') else ImageFont.load_default()
        draw.text((padding + 5, 10), info_header, font=font_h, fill=(220, 220, 220))

        for i, (ts, f_rgb) in enumerate(frames):
            c = i % cols
            r = i // cols
            x = padding + c * (thumb_w + padding)
            y = 35 + padding + r * (thumb_h + padding)

            im = Image.fromarray(f_rgb).resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(im, (x, y))

            # Позначка таймкоду на кадрі
            badge_text = f"{ts}s"
            draw.rectangle([(x + 4, y + 4), (x + 55, y + 24)], fill=(0, 0, 0))
            draw.text((x + 8, y + 6), badge_text, fill=(255, 255, 255))

        sheet.save(output_sheet_path, quality=85)
        return True

    except Exception as e:
        logger.error(f"Помилка створення контактної сітки для {video_path}: {e}")
        return False

def download_video(video_item: dict, out_dir: str, sheets_dir: str, step_sec_override: int | None, generate_sheet: bool, logger: logging.Logger, retries: int = 3) -> bool:
    item_id = video_item.get("id", "unknown")
    title = video_item.get("title", item_id)
    download_url = video_item.get("download_url")

    if not download_url:
        logger.warning(f"[{item_id}] Відсутнє посилання для завантаження: {title}")
        return False

    safe_title = sanitize_filename(title)
    final_filename = f"{safe_title}_{item_id}.mp4"
    final_path = os.path.join(out_dir, final_filename)
    part_path = final_path + ".part"
    sheet_filename = f"{safe_title}_{item_id}_sheet.jpg"
    sheet_path = os.path.join(sheets_dir, sheet_filename)

    already_downloaded = os.path.exists(final_path) and os.path.getsize(final_path) > 1024

    if already_downloaded:
        logger.info(f"[{item_id}] Файл вже існує, пропуск завантаження: {final_filename}")
        video_item["local_path"] = final_path
        video_item["status"] = "already_exists"
    else:
        session = get_thread_session()
        success = False

        for attempt in range(1, retries + 1):
            try:
                logger.info(f"[{item_id}] Початок завантаження (спроба {attempt}): {final_filename}")
                resp = session.get(download_url, stream=True, timeout=60)
                if resp.status_code != 200:
                    logger.warning(f"[{item_id}] Помилка сервера {resp.status_code} для {download_url}")
                    time.sleep(2)
                    continue

                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(part_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                if total_size > 0 and downloaded < total_size:
                    logger.warning(f"[{item_id}] Завантажено неповний файл ({downloaded}/{total_size}), повтор...")
                    if os.path.exists(part_path):
                        os.remove(part_path)
                    time.sleep(2)
                    continue

                if os.path.exists(part_path):
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    os.rename(part_path, final_path)

                file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
                logger.info(f"[{item_id}] УСПІШНО завантажено ({file_size_mb:.2f} MB): {final_filename}")
                video_item["local_path"] = final_path
                video_item["status"] = "downloaded"
                video_item["file_size_bytes"] = os.path.getsize(final_path)
                success = True
                break

            except Exception as e:
                logger.error(f"[{item_id}] Помилка під час завантаження (спроба {attempt}/{retries}): {e}")
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                time.sleep(2 * attempt)

        if not success:
            video_item["status"] = "failed"
            return False

    # Генерація контактної сітки кадрів
    if generate_sheet and HAS_CV2_PIL:
        if not os.path.exists(sheet_path):
            sheet_ok = generate_contact_sheet(final_path, sheet_path, step_sec_override, logger)
            if sheet_ok:
                logger.info(f"[{item_id}] Створено контактну сітку кадрів: {sheet_filename}")
                video_item["contact_sheet_path"] = sheet_path
        else:
            video_item["contact_sheet_path"] = sheet_path

    return True

def process_item_url(item: dict, logger: logging.Logger) -> dict:
    session = get_thread_session()
    item_id = item.get("id")
    title = item.get("title", "")
    author = item.get("author", "")
    item_url = item.get("itemUrl", "")
    full_url = urljoin(BASE_DOMAIN, item_url)

    unwatermarked_url = fetch_unwatermarked_url(session, item, logger)

    return {
        "id": item_id,
        "title": title,
        "author": author,
        "item_page_url": full_url,
        "download_url": unwatermarked_url,
        "aspect_ratio": item.get("aspectRatio"),
        "duration": item.get("trackDurations") or item.get("videoLength")
    }

def main():
    parser = argparse.ArgumentParser(
        description="Багатопотоковий фоновий скрапер відео з Envato Elements (без водяних знаків, вибір 16:9 / 9:16 та сітка кадрів)"
    )
    parser.add_argument(
        "--url", "-u",
        type=str,
        default="https://elements.envato.com/stock-video/hello+kitty",
        help="URL пошуку/категорії або пошуковий запит (наприклад, 'hello kitty')"
    )
    parser.add_argument(
        "--aspect-ratio", "-ar",
        type=str,
        choices=["16:9", "9:16", "all"],
        default="all",
        help="Співвідношення сторін: '16:9' (горизонтальні), '9:16' (вертикальні/shorts/reels) або 'all'"
    )
    parser.add_argument(
        "--pages", "-p",
        type=str,
        default="1",
        help="Кількість сторінок для обробки (ціле число або 'all')"
    )
    parser.add_argument(
        "--threads", "-t",
        type=int,
        default=5,
        help="Кількість паралельних потоків завантаження (за замовчуванням 5)"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="./downloads",
        help="Папка для збереження відео (за замовчуванням './downloads')"
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="Максимальна кількість відео для завантаження"
    )
    parser.add_argument(
        "--frame-step",
        type=int,
        default=None,
        help="Крок у секундах для вилучення кадрів у сітку (якщо не вказано, обчислюється автоматично: 9с->1с, 18с->2с)"
    )
    parser.add_argument(
        "--no-sheet",
        action="store_true",
        help="Вимкнути створення контактної сітки кадрів"
    )
    parser.add_argument(
        "--log",
        type=str,
        default="scraper.log",
        help="Файл для запису журналу дій (за замовчуванням 'scraper.log')"
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Тільки зібрати метадані та посилання без завантаження самих файлів"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Увімкнути детальні повідомлення налагодження"
    )

    args = parser.parse_args()

    orientation = normalize_aspect_ratio_param(args.aspect_ratio)
    sheets_dir = os.path.join(args.output, "sheets")
    os.makedirs(args.output, exist_ok=True)
    if not args.no_sheet:
        os.makedirs(sheets_dir, exist_ok=True)

    logger = setup_logger(args.log, verbose=args.verbose)

    logger.info("=" * 60)
    logger.info("ЗАПУСК СКРАПЕРА ENVATO ELEMENTS")
    logger.info(f"Ціль: {args.url}")
    logger.info(f"Співвідношення сторін: {args.aspect_ratio} ({orientation})")
    logger.info(f"Сторінки: {args.pages} | Потоки: {args.threads} | Ліміт: {args.limit}")
    logger.info(f"Контактна сітка кадрів: {'ВИМКНЕНО' if args.no_sheet else 'УВІМКНЕНО'}")
    logger.info(f"Папка виводу: {os.path.abspath(args.output)}")
    logger.info("=" * 60)

    session = requests.Session(impersonate="chrome")
    base_search_url = resolve_search_url(args.url, orientation)

    # 1. Збір карток зі сторінок пошуку
    all_raw_items = []
    current_page = 1
    max_pages = 1

    if args.pages.lower() == "all":
        max_pages = 999
    else:
        try:
            max_pages = int(args.pages)
        except ValueError:
            max_pages = 1

    while current_page <= max_pages:
        page_url = build_page_url(base_search_url, current_page)
        items, pagination = fetch_page_items(session, page_url, logger)

        if not items:
            logger.info(f"На сторінці {current_page} не знайдено елементів або досягнуто кінця.")
            break

        filtered_items = [it for it in items if is_matching_aspect_ratio(it, orientation)]
        all_raw_items.extend(filtered_items)
        logger.info(f"Сторінка {current_page}: знайдено {len(items)} відео, після фільтра орієнтації: {len(filtered_items)} (всього в черзі: {len(all_raw_items)})")

        if args.limit and len(all_raw_items) >= args.limit:
            all_raw_items = all_raw_items[:args.limit]
            logger.info(f"Досягнуто встановленого ліміту {args.limit} відео.")
            break

        total_pages = pagination.get("totalPages", 1)
        if current_page >= total_pages:
            logger.info(f"Досягнуто останньої доступної сторінки ({total_pages}).")
            break

        current_page += 1
        time.sleep(1)

    logger.info(f"Всього відібрано {len(all_raw_items)} відео для обробки.")
    if not all_raw_items:
        logger.warning("Список відео порожній. Роботу завершено.")
        return

    # 2. Отримання прямих unwatermarked посилань у багатопотоковому режимі
    logger.info(f"Отримання прямих посилань без водяного знаку у {args.threads} потоків...")
    processed_items = []

    with ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="MetaWorker") as executor:
        futures = {
            executor.submit(process_item_url, item, logger): item
            for item in all_raw_items
        }

        for fut in as_completed(futures):
            try:
                res = fut.result()
                processed_items.append(res)
                dl_status = "ЗНАЙДЕНО" if res.get("download_url") else "НЕ ЗНАЙДЕНО"
                logger.info(f"[{len(processed_items)}/{len(all_raw_items)}] {res['title']} -> {dl_status}")
            except Exception as e:
                logger.error(f"Помилка обробки елемента: {e}")

    # Збереження проміжного маніфесту
    manifest_path = os.path.join(args.output, "metadata.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(processed_items, f, indent=2, ensure_ascii=False)
    logger.info(f"Метадані збережено у {manifest_path}")

    if args.skip_download:
        logger.info("Прапорець --skip-download активний. Завантаження відео пропущено.")
        return

    # 3. Багатопотокове завантаження MP4 файлів та створення сітки кадрів
    valid_downloads = [it for it in processed_items if it.get("download_url")]
    logger.info(f"Початок скачування {len(valid_downloads)} відео у {args.threads} потоків...")

    successful_count = 0
    with ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="DLWorker") as executor:
        futures = {
            executor.submit(
                download_video,
                item,
                args.output,
                sheets_dir,
                args.frame_step,
                not args.no_sheet,
                logger
            ): item
            for item in valid_downloads
        }

        for fut in as_completed(futures):
            try:
                success = fut.result()
                if success:
                    successful_count += 1
            except Exception as e:
                logger.error(f"Помилка завантажувача: {e}")

    # Оновлення остаточного маніфесту
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(processed_items, f, indent=2, ensure_ascii=False)

    logger.info("=" * 60)
    logger.info(f"РОБОТУ ЗАВЕРШЕНО!")
    logger.info(f"Успішно завантажено: {successful_count}/{len(valid_downloads)}")
    logger.info(f"Файли відео: {os.path.abspath(args.output)}")
    if not args.no_sheet:
        logger.info(f"Контактні сітки кадрів: {os.path.abspath(sheets_dir)}")
    logger.info(f"Журнал дій: {os.path.abspath(args.log)}")
    logger.info("=" * 60)

if __name__ == "__main__":
    main()
