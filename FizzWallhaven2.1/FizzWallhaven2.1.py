import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from PIL import Image
from io import BytesIO
import webbrowser

# ==================== 核心配置区域 ====================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

MAIN_FONT = ("Microsoft YaHei", 12)
FONT_BOLD = ("Microsoft YaHei", 12, "bold")
CONFIG_FILE = "config.txt"
MIN_WINDOW_WIDTH = 1080
MIN_WINDOW_HEIGHT = 680

# 右侧预览固定按 Wallhaven 一页显示，通常 24 张
PREVIEW_PAGE_SIZE = 24
PREVIEW_CACHE_LIMIT = 6
THUMB_WORKERS = 4
THUMB_WIDTH = 196
THUMB_HEIGHT = 110
THUMB_CELL_WIDTH = 216
# ====================================================

downloaded_count = 0
is_paused = False
stop_flag = False
executor = None

download_lock = threading.Lock()
retry_queue_lock = threading.Lock()

# 本轮下载中，429 限流提示只显示一次
rate_limit_warning_shown = False
rate_limit_warning_lock = threading.Lock()

# 当前右侧预览页
current_preview_list = []
current_page = 1
last_page = None
is_page_loading = False
page_cache = {}

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


def load_saved_key():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def save_key(api_key):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(api_key.strip())
    except Exception:
        pass


def build_wallhaven_params(api_params, page_num):
    params = {
        "q": api_params["keyword"],
        "sorting": api_params["sorting"],
        "categories": api_params["categories"],
        "purity": api_params["purity"],
        "page": page_num,
        "apikey": api_params["apikey"],
    }

    if api_params["ratios"]:
        params["ratios"] = api_params["ratios"]

    # 分辨率选择 All 时，不传 atleast
    if api_params["atleast"]:
        params["atleast"] = api_params["atleast"]

    return params


def log_rate_limit_once(log_callback):
    """
    本轮下载中，429 限流提示只显示一次。
    不降低线程数，不改变总体下载策略。
    """
    global rate_limit_warning_shown

    with rate_limit_warning_lock:
        if not rate_limit_warning_shown:
            rate_limit_warning_shown = True
            log_callback("⚠️ Wallhaven 请求过快，部分图片将自动进入补偿下载。该提示本轮只显示一次。")


def mark_one_done(total_limit, log_callback, progress_callback, message):
    """
    线程安全地把一个任务计入完成。
    用于成功、已存在跳过、无法下载等情况。
    """
    global downloaded_count

    with download_lock:
        downloaded_count += 1
        done = downloaded_count

    log_callback(message.format(done=done, total=total_limit))
    progress_callback(done, total_limit)


def download_image_fast_first_pass(
    img_url,
    file_name,
    save_folder,
    total_limit,
    log_callback,
    progress_callback,
    retry_queue
):
    """
    第一阶段：高速下载。
    - 已存在：直接跳过并计入完成
    - 成功：计入完成
    - 429 / 网络波动 / 服务器临时错误：放入补偿队列，不长时间阻塞主下载线程
    - 403 / 404：通常表示资源不可用，直接计入完成并报错
    """
    global is_paused, stop_flag

    if stop_flag:
        return

    file_path = os.path.join(save_folder, file_name)

    # 已存在同名且大小正常，直接跳过
    if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
        mark_one_done(
            total_limit,
            log_callback,
            progress_callback,
            f"⏭️ [已存在跳过] ({{done}}/{{total}}) -> {file_name}"
        )
        return

    while is_paused and not stop_flag:
        time.sleep(0.3)

    if stop_flag:
        return

    try:
        res = requests.get(img_url, headers=HEADERS, timeout=18)

        if res.status_code == 200:
            with open(file_path, "wb") as f:
                f.write(res.content)

            mark_one_done(
                total_limit,
                log_callback,
                progress_callback,
                f"✨ [下载成功] ({{done}}/{{total}}) -> {file_name}"
            )
            return

        elif res.status_code == 429:
            log_rate_limit_once(log_callback)
            with retry_queue_lock:
                retry_queue.append((img_url, file_name))
            return

        elif res.status_code in (500, 502, 503, 504):
            with retry_queue_lock:
                retry_queue.append((img_url, file_name))
            return

        else:
            mark_one_done(
                total_limit,
                log_callback,
                progress_callback,
                f"❌ [无法下载] 状态码 {res.status_code}，已跳过 -> {file_name}"
            )
            return

    except Exception:
        with retry_queue_lock:
            retry_queue.append((img_url, file_name))
        return


def download_image_retry_until_success(
    img_url,
    file_name,
    save_folder,
    total_limit,
    log_callback,
    progress_callback
):
    """
    第二阶段：补偿下载。
    - 只处理第一阶段没成功的图片
    - 429 不跳过，等待后继续重试
    - 网络波动不跳过，继续重试
    - 已存在则跳过
    - 直到成功、资源不可用，或用户手动停止
    """
    global is_paused, stop_flag

    if stop_flag:
        return

    file_path = os.path.join(save_folder, file_name)

    # 可能第一阶段后文件已经存在，避免重复下载
    if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
        mark_one_done(
            total_limit,
            log_callback,
            progress_callback,
            f"⏭️ [补偿跳过已存在] ({{done}}/{{total}}) -> {file_name}"
        )
        return

    retry_times = 0

    while not stop_flag:
        while is_paused and not stop_flag:
            time.sleep(0.5)

        if stop_flag:
            return

        try:
            res = requests.get(img_url, headers=HEADERS, timeout=25)

            if res.status_code == 200:
                with open(file_path, "wb") as f:
                    f.write(res.content)

                mark_one_done(
                    total_limit,
                    log_callback,
                    progress_callback,
                    f"✨ [补偿成功] ({{done}}/{{total}}) -> {file_name}"
                )
                return

            elif res.status_code == 429:
                log_rate_limit_once(log_callback)

                retry_after = res.headers.get("Retry-After")
                try:
                    wait_time = float(retry_after) if retry_after else 3
                except ValueError:
                    wait_time = 3

                wait_time = max(3, min(wait_time, 15))
                time.sleep(wait_time)
                continue

            elif res.status_code in (500, 502, 503, 504):
                retry_times += 1
                time.sleep(min(2 + retry_times, 8))
                continue

            else:
                mark_one_done(
                    total_limit,
                    log_callback,
                    progress_callback,
                    f"❌ [无法下载] 状态码 {res.status_code}，已跳过 -> {file_name}"
                )
                return

        except Exception:
            retry_times += 1
            time.sleep(min(2 + retry_times, 8))
            continue


def trim_page_cache():
    while len(page_cache) > PREVIEW_CACHE_LIMIT:
        try:
            page_cache.pop(next(iter(page_cache)))
        except StopIteration:
            break


def load_and_render_thumb(thumb_url, full_res_url, app_instance, index, page_num, generation):
    """
    下载缩略图并渲染。
    page_num 用于防止旧页面的异步缩略图误渲染到新页面。
    """
    try:
        if generation != app_instance.preview_generation:
            return

        res = requests.get(thumb_url, headers=HEADERS, timeout=8)

        if res.status_code == 200:
            img_data = Image.open(BytesIO(res.content)).convert("RGB")

            if generation != app_instance.preview_generation:
                return

            app_instance.root.after(
                0,
                app_instance.add_thumb_to_grid,
                img_data,
                index,
                full_res_url,
                page_num,
                generation
            )

    except Exception:
        pass


def preview_page_logic(total_limit, api_params, page_to_load, app_instance, log_callback, finish_callback, generation):
    """
    右侧预览：只加载指定页，不做瀑布流。
    """
    global current_preview_list, current_page, last_page
    global is_page_loading, page_cache

    try:
        if generation != app_instance.preview_generation:
            return

        if page_to_load in page_cache:
            cached_items = page_cache[page_to_load]
            if generation != app_instance.preview_generation:
                return
            current_page = page_to_load
            current_preview_list = [item["full_url"] for item in cached_items]

            log_callback(f"📄 已从缓存切换到第 {page_to_load} 页，共 {len(cached_items)} 张。")
            app_instance.root.after(0, app_instance.render_page_items, page_to_load, cached_items, generation)
            return

        log_callback(f"🔍 正在请求 Wallhaven 第 {page_to_load} 页预览...")

        params = build_wallhaven_params(api_params, page_to_load)

        res = requests.get(
            "https://wallhaven.cc/api/v1/search",
            headers=HEADERS,
            params=params,
            timeout=12
        )

        if generation != app_instance.preview_generation:
            return

        if res.status_code != 200:
            if res.status_code == 401:
                log_callback("❌ API Key 校验失败！请检查你的 Wallhaven API Key。")
            else:
                log_callback(f"❌ Wallhaven 请求失败，状态码: {res.status_code}")
            return

        data = res.json()
        if generation != app_instance.preview_generation:
            return

        image_list = data.get("data", [])
        meta = data.get("meta", {})
        last_page = meta.get("last_page", 1)

        if not image_list:
            log_callback("🏁 没有更多壁纸了。")
            return

        page_items = []

        for img_info in image_list:
            if len(page_items) >= total_limit:
                break

            full_res_url = img_info.get("path")
            thumb_url = img_info.get("thumbs", {}).get("small")

            if not full_res_url or not thumb_url:
                continue

            page_items.append({
                "full_url": full_res_url,
                "thumb_url": thumb_url
            })

        if not page_items:
            log_callback("⚠️ 这一页没有可用缩略图。")
            return

        page_cache[page_to_load] = page_items
        trim_page_cache()
        current_page = page_to_load
        current_preview_list = [item["full_url"] for item in page_items]

        log_callback(f"🖼️ 第 {page_to_load} 页加载成功！当前页共 {len(current_preview_list)} 张。")
        app_instance.root.after(0, app_instance.render_page_items, page_to_load, page_items, generation)

    except Exception as e:
        log_callback(f"⚠️ [网络提示] 页面加载失败: {e}")

    finally:
        if generation == app_instance.preview_generation:
            is_page_loading = False
            finish_callback(generation)


def download_url_list_logic(urls, save_folder, max_workers, log_callback, progress_callback, finish_callback, task_name):
    """
    两阶段下载：
    第一阶段：高速并发下载，不让 429 长时间占用主线程
    第二阶段：自动补偿下载 429 / 网络失败的图片，直到成功或用户停止
    """
    global downloaded_count, is_paused, stop_flag, executor
    global rate_limit_warning_shown

    downloaded_count = 0
    stop_flag = False
    is_paused = False
    rate_limit_warning_shown = False

    total_limit = len(urls)

    if total_limit == 0:
        log_callback("⚠️ 没有可下载的壁纸。")
        finish_callback()
        return

    log_callback(f"🚀 开始{task_name}，共 {total_limit} 张壁纸...")
    log_callback("⚡ 第一阶段：高速下载中...")

    retry_queue = []

    executor = ThreadPoolExecutor(max_workers=max_workers)

    try:
        futures = []

        for full_res_url in urls:
            if stop_flag:
                break

            while is_paused and not stop_flag:
                time.sleep(0.2)

            file_name = full_res_url.split("/")[-1]

            futures.append(
                executor.submit(
                    download_image_fast_first_pass,
                    full_res_url,
                    file_name,
                    save_folder,
                    total_limit,
                    log_callback,
                    progress_callback,
                    retry_queue
                )
            )

        for f in futures:
            if stop_flag:
                break
            try:
                f.result()
            except Exception:
                pass

    finally:
        try:
            executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass

    if stop_flag:
        finish_callback()
        return

    if retry_queue:
        log_callback(f"🔁 第一阶段完成，有 {len(retry_queue)} 张需要自动补偿下载。")
        log_callback("🧩 第二阶段：开始自动补偿下载，不需要手动重新点击。")

        retry_workers = min(max_workers, 8)
        executor = ThreadPoolExecutor(max_workers=retry_workers)

        try:
            retry_futures = []

            for img_url, file_name in retry_queue:
                if stop_flag:
                    break

                retry_futures.append(
                    executor.submit(
                        download_image_retry_until_success,
                        img_url,
                        file_name,
                        save_folder,
                        total_limit,
                        log_callback,
                        progress_callback
                    )
                )

            for f in retry_futures:
                if stop_flag:
                    break
                try:
                    f.result()
                except Exception:
                    pass

        finally:
            try:
                executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                pass
    else:
        log_callback("✅ 第一阶段全部成功，无需补偿下载。")

    finish_callback()


def batch_download_logic(target_count, api_params, save_folder, max_workers, log_callback, progress_callback, finish_callback):
    """
    左侧批量下载：不依赖右侧预览。
    会自动跨页收集链接，直到达到用户设置的下载数量，比如 100 / 500。
    """
    global downloaded_count, is_paused, stop_flag, executor
    global rate_limit_warning_shown

    downloaded_count = 0
    stop_flag = False
    is_paused = False
    rate_limit_warning_shown = False

    collected_urls = []
    seen_urls = set()

    page_num = 1
    max_safe_pages = 1000

    log_callback(f"🔎 开始批量收集壁纸链接，目标数量: {target_count} 张...")

    try:
        while len(collected_urls) < target_count and not stop_flag:
            while is_paused and not stop_flag:
                time.sleep(0.2)

            if page_num > max_safe_pages:
                log_callback("⚠️ 已达到安全页数上限，停止继续收集。")
                break

            params = build_wallhaven_params(api_params, page_num)

            log_callback(f"📡 正在读取第 {page_num} 页... 当前已收集 {len(collected_urls)}/{target_count} 张。")

            res = requests.get(
                "https://wallhaven.cc/api/v1/search",
                headers=HEADERS,
                params=params,
                timeout=15
            )

            if res.status_code != 200:
                if res.status_code == 401:
                    log_callback("❌ API Key 校验失败！请检查你的 Wallhaven API Key。")
                else:
                    log_callback(f"❌ Wallhaven 请求失败，状态码: {res.status_code}")
                break

            data = res.json()
            image_list = data.get("data", [])
            meta = data.get("meta", {})
            api_last_page = meta.get("last_page", 1)

            if not image_list:
                log_callback("🏁 没有更多搜索结果了。")
                break

            for img_info in image_list:
                full_res_url = img_info.get("path")

                if not full_res_url:
                    continue

                if full_res_url in seen_urls:
                    continue

                seen_urls.add(full_res_url)
                collected_urls.append(full_res_url)

                if len(collected_urls) >= target_count:
                    break

            if api_params["sorting"] != "random" and page_num >= api_last_page:
                log_callback("🏁 已经到达最后一页，无法继续收集更多。")
                break

            page_num += 1

        if stop_flag:
            log_callback("🛑 批量任务已停止。")
            finish_callback()
            return

        if not collected_urls:
            log_callback("⚠️ 没有收集到可下载的壁纸。")
            finish_callback()
            return

        if len(collected_urls) < target_count:
            log_callback(f"⚠️ 只收集到 {len(collected_urls)} 张，少于目标 {target_count} 张。")

        download_url_list_logic(
            collected_urls,
            save_folder,
            max_workers,
            log_callback,
            progress_callback,
            finish_callback,
            task_name="批量下载"
        )

    except Exception as e:
        log_callback(f"⚠️ [批量下载错误] {e}")
        finish_callback()


class FizzWallhavenGUIv2:
    def __init__(self, root):
        self.root = root
        self.root.title("FizzWallhaven 2.1")
        self.root.geometry(f"{MIN_WINDOW_WIDTH}x{MIN_WINDOW_HEIGHT}")
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.root.resizable(True, True)

        self.active_api_params = None
        self.thumb_executor = None
        self.active_render_page = 1
        self.active_render_generation = 0
        self.preview_generation = 0
        self.is_fullscreen = False
        self.active_download_total = 0
        self.active_download_mode = ""

        # 🔑 API Key
        ctk.CTkLabel(root, text="Wallhaven API Key:", font=FONT_BOLD).place(x=20, y=18)
        self.entry_key = ctk.CTkEntry(root, width=320, font=MAIN_FONT, show="*")
        self.entry_key.place(x=150, y=15)

        saved_key = load_saved_key()
        if saved_key:
            self.entry_key.insert(0, saved_key)

        self.btn_show_key = ctk.CTkButton(
            root,
            text="👁️ 显示",
            font=MAIN_FONT,
            command=self.toggle_key_visibility,
            width=60
        )
        self.btn_show_key.place(x=485, y=15)

        # 📂 保存文件夹
        ctk.CTkLabel(root, text="保存文件夹:", font=MAIN_FONT).place(x=20, y=55)
        self.entry_folder = ctk.CTkEntry(root, width=380, font=MAIN_FONT)
        self.entry_folder.place(x=100, y=52)

        self.btn_browse = ctk.CTkButton(
            root,
            text="选择...",
            font=MAIN_FONT,
            command=self.browse_folder,
            width=70
        )
        self.btn_browse.place(x=495, y=52)

        # 🔍 搜索关键词
        ctk.CTkLabel(root, text="🔍 搜索关键词:", font=MAIN_FONT).place(x=20, y=95)
        self.entry_keyword = ctk.CTkEntry(
            root,
            width=330,
            font=MAIN_FONT,
            placeholder_text="输入英文关键词（如 GTA, cyberpunk, car... 空置则全局泛搜）"
        )
        self.entry_keyword.place(x=125, y=92)

        self.btn_preview = ctk.CTkButton(
            root,
            text="搜索并预览",
            fg_color="#3498DB",
            hover_color="#2980B9",
            text_color="white",
            font=FONT_BOLD,
            width=100,
            command=self.start_preview
        )
        self.btn_preview.place(x=465, y=92)

        # ⚙️ 基本下载配置
        ctk.CTkLabel(root, text="下载数量:", font=MAIN_FONT).place(x=20, y=135)
        self.entry_count = ctk.CTkEntry(root, width=70, font=MAIN_FONT)
        self.entry_count.place(x=85, y=132)
        self.entry_count.insert(0, "100")

        ctk.CTkLabel(root, text="线程数:", font=MAIN_FONT).place(x=170, y=135)
        self.entry_workers = ctk.CTkEntry(root, width=50, font=MAIN_FONT)
        self.entry_workers.place(x=215, y=132)
        self.entry_workers.insert(0, "16")

        ctk.CTkLabel(root, text="比例:", font=MAIN_FONT).place(x=280, y=135)
        self.combo_ratio = ctk.CTkComboBox(
            root,
            values=["16x9", "16x10", "21x9", "4x3", "1x1", "All"],
            font=MAIN_FONT,
            dropdown_font=MAIN_FONT,
            width=80
        )
        self.combo_ratio.place(x=315, y=132)
        self.combo_ratio.set("16x9")

        ctk.CTkLabel(root, text="分辨率:", font=MAIN_FONT).place(x=415, y=135)
        self.combo_res = ctk.CTkComboBox(
            root,
            values=["All", "1920x1080", "2560x1440", "3840x2160", "5120x2880"],
            font=MAIN_FONT,
            dropdown_font=MAIN_FONT,
            width=110
        )
        self.combo_res.place(x=465, y=132)
        self.combo_res.set("2560x1440")

        # 📊 表单选项容器
        self.form_frame = ctk.CTkFrame(root, fg_color="transparent")
        self.form_frame.place(x=20, y=175)

        # Sorting
        ctk.CTkLabel(
            self.form_frame,
            text="Sorting:",
            font=FONT_BOLD,
            width=85,
            anchor="w"
        ).grid(row=0, column=0, padx=(0, 10), pady=6, sticky="w")

        self.var_sorting = tk.StringVar(value="toplist")
        modes = [
            ("Latest", "date_added"),
            ("Hot", "hot"),
            ("Toplist", "toplist"),
            ("Random", "random")
        ]

        for i, (text, value) in enumerate(modes):
            ctk.CTkRadioButton(
                self.form_frame,
                text=text,
                font=MAIN_FONT,
                variable=self.var_sorting,
                value=value
            ).grid(row=0, column=i + 1, padx=(5, 22), pady=6, sticky="w")

        # Categories
        ctk.CTkLabel(
            self.form_frame,
            text="Categories:",
            font=FONT_BOLD,
            width=85,
            anchor="w"
        ).grid(row=1, column=0, padx=(0, 10), pady=6, sticky="w")

        self.var_gen = tk.BooleanVar(value=True)
        self.var_anim = tk.BooleanVar(value=True)
        self.var_peo = tk.BooleanVar(value=False)

        ctk.CTkCheckBox(
            self.form_frame,
            text="General",
            font=MAIN_FONT,
            variable=self.var_gen
        ).grid(row=1, column=1, padx=(5, 22), pady=6, sticky="w")

        ctk.CTkCheckBox(
            self.form_frame,
            text="Anime",
            font=MAIN_FONT,
            variable=self.var_anim
        ).grid(row=1, column=2, padx=(5, 22), pady=6, sticky="w")

        ctk.CTkCheckBox(
            self.form_frame,
            text="People",
            font=MAIN_FONT,
            variable=self.var_peo
        ).grid(row=1, column=3, padx=(5, 22), pady=6, sticky="w")

        # Purity
        ctk.CTkLabel(
            self.form_frame,
            text="Purity:",
            font=FONT_BOLD,
            width=85,
            anchor="w"
        ).grid(row=2, column=0, padx=(0, 10), pady=6, sticky="w")

        self.var_sfw = tk.BooleanVar(value=True)
        self.var_sketchy = tk.BooleanVar(value=False)
        self.var_nsfw = tk.BooleanVar(value=False)

        ctk.CTkCheckBox(
            self.form_frame,
            text="SFW",
            font=MAIN_FONT,
            variable=self.var_sfw
        ).grid(row=2, column=1, padx=(5, 22), pady=6, sticky="w")

        ctk.CTkCheckBox(
            self.form_frame,
            text="Sketchy",
            font=MAIN_FONT,
            variable=self.var_sketchy
        ).grid(row=2, column=2, padx=(5, 22), pady=6, sticky="w")

        ctk.CTkCheckBox(
            self.form_frame,
            text="NSFW",
            font=MAIN_FONT,
            variable=self.var_nsfw
        ).grid(row=2, column=3, padx=(5, 22), pady=6, sticky="w")

        # 📄 左侧状态栏与日志
        self.lbl_status = ctk.CTkLabel(
            root,
            text="当前状态: 等待启动",
            text_color="#3498DB",
            font=FONT_BOLD
        )
        self.lbl_status.place(x=20, y=295)

        self.txt_log = ctk.CTkTextbox(
            root,
            width=540,
            height=240,
            font=("Consolas", 11)
        )
        self.txt_log.place(x=20, y=325)

        # 🖼️ 右侧预览区
        self.preview_label = ctk.CTkLabel(
            root,
            text="Wallpaper Preview (💡双击看大图 | 右键选择下载)",
            font=FONT_BOLD,
            text_color="#2ECC71"
        )
        self.preview_label.place(x=590, y=18)

        self.scroll_frame = ctk.CTkScrollableFrame(
            root,
            width=440,
            height=485,
            corner_radius=12
        )
        self.scroll_frame.place(x=590, y=52)

        self.place_holder = ctk.CTkLabel(
            self.scroll_frame,
            text="点击左下角‘搜索并生成预览’\n右侧可分页浏览壁纸\n下方按钮可下载当前页",
            font=MAIN_FONT,
            text_color="gray"
        )
        self.place_holder.pack(pady=200)

        self.image_cache = {}
        self.thumb_widgets = {}
        self.preview_grid_columns = 2

        # 右侧分页与当前页下载按钮
        self.lbl_page_info = ctk.CTkLabel(
            root,
            text="第 - 页",
            font=FONT_BOLD,
            text_color="#2ECC71"
        )
        self.lbl_page_info.place(x=785, y=543)

        self.btn_prev_page = ctk.CTkButton(
            root,
            text="⬅ 上一页",
            font=FONT_BOLD,
            width=95,
            state="disabled",
            command=self.prev_page
        )
        self.btn_prev_page.place(x=610, y=570)

        self.btn_download_current_page = ctk.CTkButton(
            root,
            text="📥 下载当前页",
            font=FONT_BOLD,
            fg_color="#2ECC71",
            hover_color="#27AE60",
            width=130,
            state="disabled",
            command=self.start_current_page_download
        )
        self.btn_download_current_page.place(x=755, y=570)

        self.btn_next_page = ctk.CTkButton(
            root,
            text="下一页 ➡",
            font=FONT_BOLD,
            width=95,
            state="disabled",
            command=self.next_page
        )
        self.btn_next_page.place(x=930, y=570)

        self.root.bind("<space>", lambda event: self.toggle_pause())
        self.root.bind("<F11>", lambda event: self.toggle_fullscreen())
        self.root.bind("<Escape>", lambda event: self.exit_fullscreen())
        self.root.bind("<Configure>", self.on_window_configure)

        self.btn_fullscreen = ctk.CTkButton(
            root,
            text="全屏",
            font=MAIN_FONT,
            width=70,
            command=self.toggle_fullscreen
        )
        self.btn_fullscreen.place(x=990, y=15)

        # 右键菜单
        self.context_menu = tk.Menu(
            root,
            tearoff=0,
            font=("Microsoft YaHei", 10),
            bg="#2C3E50",
            fg="white",
            activebackground="#2980B9"
        )
        self.context_menu.add_command(label="📥 下载这张壁纸", command=self.trigger_menu_download)
        self.targeted_url = None

        # 左侧底部按钮
        self.btn_preview_unused = ctk.CTkButton(
            root,
            text="🔍 搜索并生成预览",
            fg_color="#3498DB",
            hover_color="#2980B9",
            text_color="white",
            font=FONT_BOLD,
            width=130,
            command=self.start_preview
        )
        self.btn_preview_unused.place_forget()

        self.btn_batch_download = ctk.CTkButton(
            root,
            text="🚀 批量下载",
            fg_color="#2ECC71",
            hover_color="#27AE60",
            text_color="white",
            font=FONT_BOLD,
            width=130,
            command=self.start_batch_download
        )
        self.btn_batch_download.place(x=20, y=615)

        self.btn_pause = ctk.CTkButton(
            root,
            text="⏸️ 暂停",
            fg_color="#E67E22",
            hover_color="#D35400",
            text_color="white",
            font=FONT_BOLD,
            width=130,
            state="disabled",
            command=self.toggle_pause
        )
        self.btn_pause.place(x=170, y=615)

        self.btn_stop = ctk.CTkButton(
            root,
            text="🛑 强行停止",
            fg_color="#E74C3C",
            hover_color="#C0392B",
            text_color="white",
            font=FONT_BOLD,
            width=130,
            state="disabled",
            command=self.stop_task
        )
        self.btn_stop.place(x=320, y=615)
        self.layout_widgets()

    # ==================== 右侧预览页逻辑 ====================

    def on_window_configure(self, event):
        if event.widget is self.root:
            self.layout_widgets()

    def toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
        self.root.attributes("-fullscreen", self.is_fullscreen)
        self.btn_fullscreen.configure(text="退出全屏" if self.is_fullscreen else "全屏")
        self.layout_widgets()

    def exit_fullscreen(self):
        if not self.is_fullscreen:
            return

        self.is_fullscreen = False
        self.root.attributes("-fullscreen", False)
        self.btn_fullscreen.configure(text="全屏")
        self.layout_widgets()

    def layout_widgets(self):
        if not all(hasattr(self, name) for name in (
            "btn_fullscreen",
            "txt_log",
            "btn_batch_download",
            "btn_pause",
            "btn_stop",
            "scroll_frame",
            "lbl_page_info",
            "btn_prev_page",
            "btn_download_current_page",
            "btn_next_page",
        )):
            return

        width = max(self.root.winfo_width(), MIN_WINDOW_WIDTH)
        height = max(self.root.winfo_height(), MIN_WINDOW_HEIGHT)

        left_width = 540
        preview_x = 590
        preview_width = max(440, width - preview_x - 20)
        preview_height = max(360, height - 195)
        preview_bottom = 52 + preview_height

        self.btn_fullscreen.place(x=width - 90, y=15)

        self.txt_log.configure(width=left_width, height=max(180, height - 440))
        self.btn_batch_download.place(x=20, y=height - 65)
        self.btn_pause.place(x=170, y=height - 65)
        self.btn_stop.place(x=320, y=height - 65)

        self.scroll_frame.configure(width=preview_width, height=preview_height)
        self.lbl_page_info.place(x=preview_x + preview_width / 2 - 30, y=preview_bottom + 12)
        self.btn_prev_page.place(x=preview_x + 20, y=preview_bottom + 39)
        self.btn_download_current_page.place(x=preview_x + preview_width / 2 - 65, y=preview_bottom + 39)
        self.btn_next_page.place(x=preview_x + preview_width - 125, y=preview_bottom + 39)
        self.reflow_thumb_grid()

    def get_preview_columns(self):
        width = max(self.root.winfo_width(), MIN_WINDOW_WIDTH)
        preview_width = max(440, width - 590 - 20)
        return max(2, int(preview_width // THUMB_CELL_WIDTH))

    def reflow_thumb_grid(self, force=False):
        if not hasattr(self, "thumb_widgets"):
            return

        columns = self.get_preview_columns()
        if not force and columns == self.preview_grid_columns:
            return

        for index, widget in sorted(self.thumb_widgets.items()):
            try:
                if widget.winfo_exists():
                    widget.grid(
                        row=index // columns,
                        column=index % columns,
                        padx=10,
                        pady=8
                    )
            except Exception:
                pass

        self.preview_grid_columns = columns

    def clear_preview_area(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        self.image_cache.clear()
        self.thumb_widgets.clear()

        if self.thumb_executor:
            try:
                self.thumb_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        self.thumb_executor = None

    def scroll_preview_to_top(self):
        try:
            if hasattr(self.scroll_frame, "_parent_canvas"):
                self.scroll_frame._parent_canvas.yview_moveto(0)
            elif hasattr(self.scroll_frame, "_canvas"):
                self.scroll_frame._canvas.yview_moveto(0)
        except Exception:
            pass

    def render_page_items(self, page_num, page_items, generation):
        global current_preview_list, current_page

        if generation != self.preview_generation:
            return

        current_page = page_num
        current_preview_list = [item["full_url"] for item in page_items]

        self.active_render_page = page_num
        self.active_render_generation = generation
        self.clear_preview_area()

        self.thumb_executor = ThreadPoolExecutor(max_workers=THUMB_WORKERS)

        for idx, item in enumerate(page_items):
            self.thumb_executor.submit(
                load_and_render_thumb,
                item["thumb_url"],
                item["full_url"],
                self,
                idx,
                page_num,
                generation
            )

        self.root.after(100, self.scroll_preview_to_top)
        self.update_page_buttons()

    def add_thumb_to_grid(self, pil_img, index, full_res_url, page_num, generation):
        if page_num != self.active_render_page or generation != self.active_render_generation:
            return

        try:
            target_w, target_h = THUMB_WIDTH, THUMB_HEIGHT
            pil_img = pil_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            ctk_img = ctk.CTkImage(
                light_image=pil_img,
                dark_image=pil_img,
                size=(target_w, target_h)
            )

            self.image_cache[index] = ctk_img

            columns = self.get_preview_columns()
            self.preview_grid_columns = columns
            row_idx = index // columns
            col_idx = index % columns

            img_label = ctk.CTkLabel(self.scroll_frame, image=ctk_img, text="")
            img_label.grid(row=row_idx, column=col_idx, padx=10, pady=8)
            self.thumb_widgets[index] = img_label
            img_label.configure(cursor="hand2")

            img_label.bind(
                "<Double-Button-1>",
                lambda event, url=full_res_url: webbrowser.open(url)
            )
            img_label.bind(
                "<Button-3>",
                lambda event, url=full_res_url: self.show_popup_menu(event, url)
            )

        except Exception as e:
            print("add_thumb_to_grid error:", e)

    def load_page(self, page_num):
        global is_page_loading, last_page

        if is_page_loading:
            return

        if page_num < 1:
            return

        sorting = self.active_api_params.get("sorting") if self.active_api_params else self.var_sorting.get()

        if sorting != "random" and last_page is not None and page_num > last_page:
            self.append_log("🏁 已经是最后一页了。")
            return

        is_page_loading = True

        self.btn_preview.configure(state="disabled")
        self.btn_batch_download.configure(state="disabled")
        self.btn_download_current_page.configure(state="disabled")
        self.btn_prev_page.configure(state="disabled")
        self.btn_next_page.configure(state="disabled")

        self.lbl_status.configure(
            text=f"当前状态: 正在加载第 {page_num} 页预览...",
            text_color="#3498DB"
        )

        api_params = self.active_api_params or self.get_api_parameters()
        generation = self.preview_generation

        threading.Thread(
            target=preview_page_logic,
            args=(
                PREVIEW_PAGE_SIZE,
                api_params,
                page_num,
                self,
                self.append_log,
                self.on_preview_finished,
                generation
            ),
            daemon=True
        ).start()

    def prev_page(self):
        global current_page

        if current_page <= 1:
            return

        self.load_page(current_page - 1)

    def next_page(self):
        global current_page

        self.load_page(current_page + 1)

    def update_page_buttons(self):
        global current_page, last_page, current_preview_list

        self.lbl_page_info.configure(text=f"第 {current_page} 页")

        if not current_preview_list:
            self.btn_prev_page.configure(state="disabled")
            self.btn_next_page.configure(state="disabled")
            self.btn_download_current_page.configure(state="disabled")
            return

        self.btn_download_current_page.configure(state="normal")

        if current_page > 1:
            self.btn_prev_page.configure(state="normal")
        else:
            self.btn_prev_page.configure(state="disabled")

        sorting = self.active_api_params.get("sorting") if self.active_api_params else self.var_sorting.get()

        if sorting == "random":
            self.btn_next_page.configure(state="normal")
        else:
            if last_page is None or current_page < last_page:
                self.btn_next_page.configure(state="normal")
            else:
                self.btn_next_page.configure(state="disabled")

    # ==================== 下载逻辑 ====================

    def start_batch_download(self):
        """
        左侧：批量下载。
        根据下载数量自动跨页收集 URL，比如下载 100 / 500 张。
        """
        global is_paused, stop_flag

        api_key = self.entry_key.get().strip()

        if not api_key:
            messagebox.showerror("错误", "请先输入你的 Wallhaven API Key！")
            return

        folder = self.entry_folder.get().strip()

        if not folder:
            messagebox.showerror("错误", "请先选择保存大图的文件夹！")
            return

        try:
            target_count = int(self.entry_count.get().strip())
            if target_count <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "下载数量必须是正整数！")
            return

        try:
            workers = int(self.entry_workers.get().strip())
            if workers <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "线程数必须是正整数！")
            return

        if not os.path.exists(folder):
            os.makedirs(folder)

        save_key(api_key)

        api_params = self.get_api_parameters()

        is_paused = False
        stop_flag = False
        self.active_download_total = target_count
        self.active_download_mode = "批量下载"

        self.set_ui_downloading_state()

        self.lbl_status.configure(
            text=f"当前状态: 正在准备批量下载 {target_count} 张...",
            text_color="#2ECC71"
        )

        threading.Thread(
            target=batch_download_logic,
            args=(
                target_count,
                api_params,
                folder,
                workers,
                self.append_log,
                self.update_progress,
                self.on_download_finished
            ),
            daemon=True
        ).start()

    def start_current_page_download(self):
        """
        右侧：只下载当前预览页。
        """
        global is_paused, stop_flag

        folder = self.entry_folder.get().strip()

        if not folder:
            messagebox.showerror("错误", "请先选择保存大图的文件夹！")
            return

        try:
            workers = int(self.entry_workers.get().strip())
            if workers <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "线程数必须是正整数！")
            return

        if not current_preview_list:
            messagebox.showerror("错误", "当前页没有可下载的壁纸！")
            return

        if not os.path.exists(folder):
            os.makedirs(folder)

        is_paused = False
        stop_flag = False

        urls = list(current_preview_list)
        self.active_download_total = len(urls)
        self.active_download_mode = "当前页下载"

        self.set_ui_downloading_state()

        self.lbl_status.configure(
            text=f"当前状态: 正在下载第 {current_page} 页的 {len(urls)} 张壁纸...",
            text_color="#2ECC71"
        )

        threading.Thread(
            target=download_url_list_logic,
            args=(
                urls,
                folder,
                workers,
                self.append_log,
                self.update_progress,
                self.on_download_finished,
                f"下载第 {current_page} 页"
            ),
            daemon=True
        ).start()

    def set_ui_downloading_state(self):
        self.btn_preview.configure(state="disabled")
        self.btn_batch_download.configure(state="disabled")
        self.btn_download_current_page.configure(state="disabled")
        self.btn_prev_page.configure(state="disabled")
        self.btn_next_page.configure(state="disabled")
        self.btn_pause.configure(state="normal", text="⏸️ 暂停", fg_color="#E67E22")
        self.btn_stop.configure(state="normal")

    # ==================== 单图下载与右键 ====================

    def show_popup_menu(self, event, url):
        self.targeted_url = url
        self.context_menu.post(event.x_root, event.y_root)

    def trigger_menu_download(self):
        if self.targeted_url:
            self.download_single_image(self.targeted_url)

    def download_single_image(self, url):
        folder = self.entry_folder.get().strip()

        if not folder:
            messagebox.showerror("错误", "请先选择保存大图的文件夹！")
            return

        if not os.path.exists(folder):
            os.makedirs(folder)

        file_name = url.split("/")[-1]
        self.append_log(f"🎯 [单张下载] 正在下载 -> {file_name}")

        threading.Thread(
            target=self._single_download_worker,
            args=(url, file_name, folder),
            daemon=True
        ).start()

    def _single_download_worker(self, url, file_name, folder):
        file_path = os.path.join(folder, file_name)

        if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
            self.append_log(f"⏭️ [单图已存在跳过] -> {file_name}")
            return

        while not stop_flag:
            try:
                res = requests.get(url, headers=HEADERS, timeout=25)

                if res.status_code == 200:
                    with open(file_path, "wb") as f:
                        f.write(res.content)

                    self.append_log(f"💖 [单图保存成功] -> {file_name}")
                    self.root.after(
                        0,
                        lambda: self.lbl_status.configure(
                            text=f"当前状态: 🎉 单张大图已保存成功！({file_name})",
                            text_color="#2ECC71"
                        )
                    )
                    return

                elif res.status_code == 429:
                    log_rate_limit_once(self.append_log)
                    time.sleep(3)
                    continue

                elif res.status_code in (500, 502, 503, 504):
                    time.sleep(2)
                    continue

                else:
                    self.append_log(f"❌ [单图下载失败] 状态码 {res.status_code} -> {file_name}")
                    return

            except Exception:
                time.sleep(2)

    # ==================== UI 工具函数 ====================

    def toggle_key_visibility(self):
        if self.entry_key.cget("show") == "*":
            self.entry_key.configure(show="")
            self.btn_show_key.configure(text="🔒 隐藏")
        else:
            self.entry_key.configure(show="*")
            self.btn_show_key.configure(text="👁️ 显示")

    def browse_folder(self):
        folder = filedialog.askdirectory()

        if folder:
            self.entry_folder.delete(0, tk.END)
            self.entry_folder.insert(0, folder)

    def append_log(self, text):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.append_log, text)
            return

        self.txt_log.insert(tk.END, text + "\n")
        self.txt_log.see(tk.END)

    def update_progress(self, current, total=None):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.update_progress, current, total)
            return

        if total is None:
            total = self.active_download_total

        mode = self.active_download_mode or "下载"
        self.lbl_status.configure(
            text=f"当前状态: {mode}中... 已完成 ({current}/{total})",
            text_color="#2ECC71"
        )

    def get_api_parameters(self):
        cat_str = (
            f"{int(self.var_gen.get())}"
            f"{int(self.var_anim.get())}"
            f"{int(self.var_peo.get())}"
        )

        pur_str = (
            f"{int(self.var_sfw.get())}"
            f"{int(self.var_sketchy.get())}"
            f"{int(self.var_nsfw.get())}"
        )

        ratio_val = self.combo_ratio.get()
        if ratio_val == "All":
            ratio_val = ""

        atleast_val = self.combo_res.get()
        if atleast_val == "All":
            atleast_val = ""

        return {
            "apikey": self.entry_key.get().strip(),
            "sorting": self.var_sorting.get(),
            "categories": cat_str,
            "purity": pur_str,
            "ratios": ratio_val,
            "atleast": atleast_val,
            "keyword": self.entry_keyword.get().strip()
        }

    # ==================== 搜索预览 ====================

    def start_preview(self):
        global current_page, last_page, page_cache
        global current_preview_list, is_page_loading

        api_key = self.entry_key.get().strip()

        if not api_key:
            messagebox.showerror("错误", "请先输入你的 Wallhaven API Key！")
            return

        save_key(api_key)

        self.active_api_params = self.get_api_parameters()
        self.preview_generation += 1
        self.active_render_generation = self.preview_generation

        current_page = 1
        last_page = None
        current_preview_list = []
        page_cache = {}
        is_page_loading = False

        self.txt_log.delete("1.0", tk.END)
        self.clear_preview_area()

        self.lbl_page_info.configure(text="第 - 页")
        self.lbl_status.configure(text="当前状态: 正在检索第 1 页预览...", text_color="#3498DB")

        self.load_page(1)

    def on_preview_finished(self, generation=None):
        global is_page_loading

        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.on_preview_finished, generation)
            return

        if generation is not None and generation != self.preview_generation:
            return

        is_page_loading = False

        self.btn_preview.configure(state="normal")
        self.btn_batch_download.configure(state="normal")

        if current_preview_list:
            self.btn_download_current_page.configure(state="normal")

            self.lbl_status.configure(
                text=f"当前状态: 图页就绪。第 {current_page} 页，共 {len(current_preview_list)} 张。",
                text_color="#2ECC71"
            )
        else:
            self.btn_download_current_page.configure(state="disabled")
            self.lbl_status.configure(
                text="当前状态: 没搜到任何壁纸，换个条件试试？",
                text_color="#E74C3C"
            )

        self.update_page_buttons()

    # ==================== 暂停 / 停止 / 下载结束 ====================

    def toggle_pause(self):
        global is_paused

        if self.btn_pause.cget("state") == "disabled":
            return

        is_paused = not is_paused

        if is_paused:
            self.btn_pause.configure(text="▶️ 恢复", fg_color="#3498DB")
            self.lbl_status.configure(text="当前状态: 下载已暂停", text_color="#E67E22")
            self.append_log("\n⏸️ [系统提示] 下载已暂停...")
        else:
            self.btn_pause.configure(text="⏸️ 暂停", fg_color="#E67E22")
            self.lbl_status.configure(text="当前状态: 正在继续下载...", text_color="#2ECC71")
            self.append_log("\n▶️ [系统提示] 下载已恢复...")

    def stop_task(self):
        global stop_flag, executor

        if messagebox.askyesno("强行终止", "确定要强行停止当前下载任务吗？"):
            stop_flag = True

            if executor:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass

            self.append_log("\n🛑 [系统提示] 下载任务正在停止...")
            self.lbl_status.configure(text="当前状态: 正在停止下载任务...", text_color="#E74C3C")
            self.btn_stop.configure(state="disabled")

    def on_download_finished(self):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.on_download_finished)
            return

        self.btn_preview.configure(state="normal")
        self.btn_batch_download.configure(state="normal")
        self.btn_pause.configure(state="disabled", text="⏸️ 暂停", fg_color="#E67E22")
        self.btn_stop.configure(state="disabled")

        self.update_page_buttons()

        if current_preview_list:
            self.btn_download_current_page.configure(state="normal")
        else:
            self.btn_download_current_page.configure(state="disabled")

        if not stop_flag:
            mode = self.active_download_mode or "下载"
            self.lbl_status.configure(
                text=f"当前状态: 📥 {mode}已完成！",
                text_color="#3498DB"
            )
            messagebox.showinfo("成功", f"{mode}已完成！")
        else:
            self.lbl_status.configure(
                text="当前状态: 下载已终止",
                text_color="#E74C3C"
            )


if __name__ == "__main__":
    root = ctk.CTk()
    app = FizzWallhavenGUIv2(root)
    root.mainloop()
