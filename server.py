import json
import logging
import os
import time
import threading
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


def log_request(task_id, payload, submit_resp, quota_before=None):
    """每次生成请求写一条详细日志到独立 JSON 文件。"""
    log_entry = {
        "task_id": task_id,
        "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
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


def poll_and_download(task_id, token):
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

            try:
                quota_before = get_quota()
                token = get_token()
                payload = {"model": model, "prompt": prompt}
                if resolution:
                    payload["size"] = resolution
                if duration:
                    payload["duration"] = int(duration)

                resp = requests.post(
                    f"{OPENAI_BASE_URL}/video/generations",
                    json=payload,
                    headers=openai_headers(token),
                )
                result = resp.json()
                task_id = result.get("task_id", "")
                if not task_id:
                    logger.warning("生成请求失败: %s", result)
                    self.send_json({"error": result}, 400)
                    return

                logger.info("任务已提交 task_id=%s prompt=%.80s model=%s quota_before=%s",
                            task_id, prompt, model,
                            quota_before.get("quota_balance") if quota_before else "N/A")
                log_request(task_id, payload, result, quota_before)

                entry = {
                    "task_id": task_id,
                    "prompt": prompt,
                    "model": model,
                    "status": "queued",
                    "progress": "0%",
                    "filename": None,
                    "created_at": int(time.time()),
                }
                with history_lock:
                    history.insert(0, entry)

                t = threading.Thread(target=poll_and_download, args=(task_id, token), daemon=True)
                t.start()

                self.send_json({"task_id": task_id})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = 8765
    print(f"Server running at http://localhost:{port}")
    HTTPServer(("localhost", port), Handler).serve_forever()
