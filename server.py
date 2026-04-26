import json
import logging
import os
import queue
import time
import threading
import uuid
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE_URL = "https://ai.leihuo.netease.com"
OPENAI_BASE_URL = "https://ai.leihuo.netease.com/v1"
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
VIDEOS_DIR = os.path.join(os.path.dirname(__file__), "videos")
LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "server.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def log_request(task_id, payload, submit_resp, quota_before=None, has_image=False):
    """每次生成请求写一条详细日志到独立 JSON 文件。"""
    log_entry = {
        "task_id": task_id,
        "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "has_image": has_image,
        "request": payload,
        "submit_response": submit_resp,
        "quota_before": quota_before,
    }
    path = os.path.join(LOGS_DIR, f"{task_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log_entry, f, ensure_ascii=False, indent=2)


def log_completion(task_id, result):
    """任务完成后把完整响应（含 token 用量、配额消耗）追加到该任务的日志文件。"""
    path = os.path.join(LOGS_DIR, f"{task_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            log_entry = json.load(f)
    except Exception:
        log_entry = {"task_id": task_id}

    data = result.get("data", {})
    inner = data.get("data", {})
    usage = inner.get("usage", {})

    # 查任务完成后的配额
    quota_after = get_quota()
    quota_before = log_entry.get("quota_before")
    quota_consumed = None
    if quota_before and quota_after:
        b = quota_before.get("quota_balance")
        a = quota_after.get("quota_balance")
        if b is not None and a is not None:
            quota_consumed = b - a

    log_entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    log_entry["status"] = data.get("status", "")
    log_entry["progress"] = data.get("progress", "")
    log_entry["duration_sec"] = inner.get("duration")
    log_entry["resolution"] = inner.get("resolution")
    log_entry["ratio"] = inner.get("ratio")
    log_entry["framespersecond"] = inner.get("framespersecond")
    log_entry["generate_audio"] = inner.get("generate_audio")
    log_entry["seed"] = inner.get("seed")
    log_entry["usage"] = usage
    log_entry["total_tokens"] = usage.get("total_tokens")
    log_entry["completion_tokens"] = usage.get("completion_tokens")
    total_quota = (quota_after or {}).get("total_quota") or (quota_before or {}).get("total_quota")
    quota_remaining = (quota_after or {}).get("quota_balance")
    consumed_pct = round(quota_consumed / total_quota * 100, 4) if quota_consumed and total_quota else None
    remaining_pct = round(quota_remaining / total_quota * 100, 2) if quota_remaining and total_quota else None

    log_entry["quota_after"] = quota_after
    log_entry["quota_consumed"] = quota_consumed
    log_entry["quota_consumed_pct"] = consumed_pct
    log_entry["quota_remaining_pct"] = remaining_pct
    log_entry["full_response"] = result

    with open(path, "w", encoding="utf-8") as f:
        json.dump(log_entry, f, ensure_ascii=False, indent=2)

    logger.info(
        "task=%s status=%s tokens=%s quota_consumed=%s(%.4f%%) quota_remaining=%.2f%% duration=%ss resolution=%s",
        task_id,
        log_entry["status"],
        log_entry["total_tokens"],
        quota_consumed,
        consumed_pct or 0,
        remaining_pct or 0,
        log_entry["duration_sec"],
        log_entry["resolution"],
    )


# In-memory history: list of task dicts
history = []
history_lock = threading.Lock()

# 串行任务队列：每次只提交一个任务，上一个下载完成后才提交下一个
task_queue = queue.Queue()


def _queue_worker():
    """后台线程，串行消费 task_queue，一个任务完成（含下载）后再处理下一个。"""
    while True:
        item = task_queue.get()
        try:
            payload, token, task_meta = item
            _submit_and_download(payload, token, task_meta)
        except Exception as e:
            logger.error("队列 worker 异常: %s", e)
        finally:
            task_queue.task_done()


threading.Thread(target=_queue_worker, daemon=True).start()


def get_token():
    cookie_value = open(COOKIES_FILE).read().strip()
    headers = {"Cookie": f"QAWEB_SESS={cookie_value}"}
    resp = requests.post(f"{BASE_URL}/webapi/ai_account/token", json={"need_prefix": False}, headers=headers)
    return resp.json().get("key_full", "")


def openai_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def get_quota():
    """查询当前账户配额，返回 (quota_balance, used_quota, total_quota)，失败返回 None。"""
    try:
        cookie_value = open(COOKIES_FILE).read().strip()
        headers = {"Cookie": f"QAWEB_SESS={cookie_value}"}
        resp = requests.post(f"{BASE_URL}/webapi/ai_account/user_info", headers=headers, timeout=10)
        data = resp.json()
        return {
            "quota_balance": data.get("quota_balance"),
            "used_quota": data.get("used_quota"),
            "total_quota": data.get("total_quota"),
        }
    except Exception as e:
        logger.warning("查询配额失败: %s", e)
        return None


def _submit_and_download(payload, token, task_meta):
    """提交任务到远端，轮询直到下载完成。由队列 worker 串行调用。"""
    pending_id = task_meta["pending_id"]

    # token/quota 在排队期间可能过期，提交前重新获取
    quota_before = None
    try:
        token = get_token()
        quota_before = get_quota()
    except Exception as e:
        logger.error("获取 token/quota 失败，任务无法提交: %s", e)
        with history_lock:
            for item in history:
                if item.get("pending_id") == pending_id:
                    item["status"] = "FAILED"
                    break
        return

    if not token:
        logger.error("token 为空，任务无法提交 pending_id=%s", pending_id)
        with history_lock:
            for item in history:
                if item.get("pending_id") == pending_id:
                    item["status"] = "FAILED"
                    break
        return

    # 打印 payload 结构用于调试，但隐藏 base64 内容（避免日志爆炸）
    def _summarize(obj):
        if isinstance(obj, dict):
            return {k: _summarize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_summarize(x) for x in obj]
        if isinstance(obj, str) and obj.startswith("data:") and len(obj) > 80:
            return f"<{obj[:30]}...{len(obj)} chars>"
        if isinstance(obj, str) and len(obj) > 200:
            return f"{obj[:200]}...({len(obj)} chars)"
        return obj

    logger.info("提交 payload: %s", json.dumps(_summarize(payload), ensure_ascii=False))

    resp = requests.post(
        f"{OPENAI_BASE_URL}/video/generations",
        json=payload,
        headers=openai_headers(token),
    )
    result = resp.json()
    task_id = result.get("task_id", "")
    if not task_id:
        logger.warning("生成请求失败: %s", result)
        with history_lock:
            for item in history:
                if item.get("pending_id") == pending_id:
                    item["status"] = "FAILED"
                    break
        return

    logger.info("任务已提交 task_id=%s prompt=%.80s model=%s quota_before=%s",
                task_id, payload.get("prompt", ""), payload.get("model", ""),
                quota_before.get("quota_balance") if quota_before else "N/A")
    log_request(task_id, payload, result, quota_before, has_image=task_meta["has_media"])

    # 更新 pending entry：替换 task_id，更新状态，不新增 entry
    with history_lock:
        for item in history:
            if item.get("pending_id") == pending_id:
                item["task_id"] = task_id
                item["status"] = "queued"
                item.pop("queue_position", None)
                break

    # 轮询直到完成，先等一段时间再开始
    time.sleep(5)
    while True:
        resp = requests.get(f"{OPENAI_BASE_URL}/video/generations/{task_id}", headers=openai_headers(token))
        result = resp.json()
        data = result.get("data", {})
        status = data.get("status", "")
        progress = data.get("progress", "0%")

        with history_lock:
            for item in history:
                if item["task_id"] == task_id:
                    item["status"] = status
                    item["progress"] = progress
                    break

        if status in ("SUCCESS", "FAILED", "CANCELLED"):
            log_completion(task_id, result)
            if status == "SUCCESS":
                video_url = data.get("data", {}).get("content", {}).get("video_url", "")
                if video_url:
                    filename = os.path.join(VIDEOS_DIR, f"{task_id}.mp4")
                    dl = requests.get(video_url, stream=True)
                    with open(filename, "wb") as f:
                        for chunk in dl.iter_content(chunk_size=8192):
                            f.write(chunk)
                    logger.info("视频已下载 task_id=%s file=%s", task_id, filename)
                    with history_lock:
                        for item in history:
                            if item["task_id"] == task_id:
                                item["filename"] = filename
                                break
            elif status == "FAILED":
                logger.error("任务失败 task_id=%s fail_reason=%s", task_id, data.get("fail_reason", ""))
            break
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, mime):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_file(os.path.join(os.path.dirname(__file__), "index.html"), "text/html; charset=utf-8")

        elif path == "/api/history":
            with history_lock:
                self.send_json(list(history))

        elif path.startswith("/api/video/"):
            task_id = path[len("/api/video/"):]
            filename = os.path.join(VIDEOS_DIR, f"{task_id}.mp4")
            if os.path.exists(filename):
                self.send_file(filename, "video/mp4")
            else:
                self.send_json({"error": "not found"}, 404)

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/generate":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            prompt = body.get("prompt", "")
            model = body.get("model", "doubao-seedance-2-0-fast-260128")
            resolution = body.get("resolution", "")
            duration = body.get("duration", "")
            images = body.get("images", [])  # list of base64 data URIs

            ratio = body.get("ratio", "16:9")
            generate_audio = body.get("generate_audio", None)
            # content items: list of {type, ...url..., role} dicts (image_url/video_url/audio_url)
            content_items = body.get("content", [])

            try:
                payload = {"model": model, "prompt": prompt}

                has_media = bool(images or content_items)

                # r2v 模式（有图片/视频）下，fast 模型不支持 duration / resolution
                if has_media and "fast" in model.lower():
                    # if duration:
                    #     logger.warning("模型 %s 在 r2v 模式下不支持 duration 参数，已忽略", model)
                    #     duration = ""
                    if resolution:
                        logger.warning("模型 %s 在 r2v 模式下不支持 resolution 参数，已忽略", model)
                        resolution = ""

                metadata = {"watermark": False}
                if resolution:
                    metadata["resolution"] = resolution
                if duration:
                    metadata["duration"] = int(duration)
                if ratio:
                    metadata["ratio"] = ratio
                if generate_audio is not None:
                    metadata["generate_audio"] = generate_audio

                # 构建 content 数组（包含 prompt text + 媒体引用）
                if has_media:
                    content = [{"type": "text", "text": prompt}]
                    for img in images:
                        content.append({
                            "type": "image_url",
                            "image_url": {"url": img},
                            "role": "reference_image",
                        })
                    content.extend(content_items)
                    metadata["content"] = content

                payload["metadata"] = metadata

                # 立即写入一条 pending 记录，让调用方可以跟踪排队状态
                pending_id = f"pending-{uuid.uuid4().hex[:12]}"
                queue_pos = task_queue.qsize() + 1
                entry = {
                    "task_id": pending_id,
                    "pending_id": pending_id,
                    "prompt": prompt,
                    "model": model,
                    "status": "pending",
                    "progress": "0%",
                    "filename": None,
                    "has_image": has_media,
                    "created_at": int(time.time()),
                    "queue_position": queue_pos,
                }
                with history_lock:
                    history.insert(0, entry)

                task_meta = {
                    "has_media": has_media,
                    "pending_id": pending_id,
                }
                task_queue.put((payload, None, task_meta))
                logger.info("任务入队 pending_id=%s queue_pos=%d has_media=%s prompt=%.80s model=%s",
                            pending_id, queue_pos, has_media, prompt, model)

                self.send_json({"task_id": pending_id, "queue_position": queue_pos})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = 8765
    print(f"Server running at http://localhost:{port}")
    HTTPServer(("localhost", port), Handler).serve_forever()
