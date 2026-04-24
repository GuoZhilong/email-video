import json
import os
import smtplib
import threading
import time
import requests
from email import message_from_bytes
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from imapclient import IMAPClient

# ── 配置 ──────────────────────────────────────────────────────────────────────
QQ_EMAIL = "786212900@qq.com"
QQ_AUTH_CODE = "vzyfehhqsgqpbcdh"

IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

API_BASE = "http://localhost:8765"
LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")
POLL_INTERVAL = 10  # 秒
# ─────────────────────────────────────────────────────────────────────────────


def send_reply(to_addr, subject, body, attachment_path=None):
    msg = MIMEMultipart()
    msg["From"] = QQ_EMAIL
    msg["To"] = to_addr
    msg["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_path:
        with open(attachment_path, "rb") as f:
            part = MIMEBase("video", "mp4")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        filename = attachment_path.split("\\")[-1].split("/")[-1]
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(QQ_EMAIL, QQ_AUTH_CODE)
        server.sendmail(QQ_EMAIL, to_addr, msg.as_string())
    print(f"[mail] 已回复 {to_addr}")


def read_quota_info(task_id):
    """从任务日志读配额消耗信息，返回格式化字符串，失败返回空字符串。"""
    try:
        path = os.path.join(LOGS_DIR, f"{task_id}.json")
        with open(path, "r", encoding="utf-8") as f:
            log = json.load(f)
        consumed = log.get("quota_consumed")
        consumed_pct = log.get("quota_consumed_pct")
        remaining_pct = log.get("quota_remaining_pct")
        if consumed is None:
            return ""
        return (
            f"\n\n--- 配额信息 ---\n"
            f"本次消耗配额：{consumed:,}（占总配额 {consumed_pct}%）\n"
            f"账户剩余配额：{remaining_pct}%"
        )
    except Exception:
        return ""


def handle_email(from_addr, subject, params):
    prompt = params["prompt"]
    print(f"[mail] 收到来自 {from_addr} 的请求: {prompt[:60]}")

    # 先回一封确认邮件
    send_reply(from_addr, subject, f"已收到您的请求，视频生成中，完成后会自动发送给您。\n\nPrompt: {prompt}")

    # 提交生成任务
    resp = requests.post(f"{API_BASE}/api/generate", json=params)
    if resp.status_code != 200 or "error" in resp.json():
        send_reply(from_addr, subject, f"视频生成提交失败: {resp.text}")
        return

    task_id = resp.json()["task_id"]
    print(f"[mail] 任务已提交 task_id={task_id}")

    # 轮询等待完成
    while True:
        time.sleep(POLL_INTERVAL)
        history = requests.get(f"{API_BASE}/api/history").json()
        item = next((i for i in history if i["task_id"] == task_id), None)
        if not item:
            continue
        status = item["status"]
        print(f"[mail] 轮询 task_id={task_id} status={status} progress={item.get('progress', '')}")
        if status == "SUCCESS":
            filename = item.get("filename")
            quota_info = read_quota_info(task_id)
            if filename:
                send_reply(from_addr, subject, f"您的视频已生成完成，请查收附件。{quota_info}", attachment_path=filename)
            else:
                send_reply(from_addr, subject, f"视频生成成功，但文件暂时无法获取，请稍后访问 http://localhost:8765 查看。{quota_info}")
            break
        elif status in ("FAILED", "CANCELLED"):
            send_reply(from_addr, subject, f"视频生成失败，请重新发送邮件重试。状态: {status}")
            break


VALID_KEYS = {"prompt", "duration", "resolution", "model"}
FORMAT_HELP = """邮件正文格式：

prompt: 视频描述内容（必填）
duration: 时长，秒，如 5（可选）
resolution: 分辨率，如 1280x720（可选）
model: 模型名称（可选）

示例：
prompt: A cat playing piano in a jazz bar
duration: 8
resolution: 1280x720"""


def parse_body(body):
    """解析 key: value 格式，返回 (params, error)。"""
    params = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            return None, f"格式错误，无法识别行：「{line}」\n\n{FORMAT_HELP}"
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key not in VALID_KEYS:
            return None, f"未知字段「{key}」，支持的字段：{', '.join(VALID_KEYS)}\n\n{FORMAT_HELP}"
        if not value:
            return None, f"字段「{key}」的值不能为空\n\n{FORMAT_HELP}"
        params[key] = value

    if "prompt" not in params:
        return None, f"缺少必填字段 prompt\n\n{FORMAT_HELP}"

    return params, None


def extract_body(mail_bytes):
    msg = message_from_bytes(mail_bytes)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                break
    else:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
    return body.strip()


def listen():
    print(f"[mail] 开始监听 {QQ_EMAIL} ...")
    while True:
        try:
            with IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
                client.login(QQ_EMAIL, QQ_AUTH_CODE)
                client.select_folder("INBOX")
                print("[mail] 已连接，等待新邮件 (IDLE)...")

                while True:
                    # 先处理已有未读邮件
                    uids = client.search(["UNSEEN"])
                    for uid in uids:
                        raw = client.fetch([uid], ["RFC822", "ENVELOPE"])
                        envelope = raw[uid][b"ENVELOPE"]
                        mail_bytes = raw[uid][b"RFC822"]

                        from_addr = envelope.from_[0].mailbox.decode() + "@" + envelope.from_[0].host.decode()
                        subject = envelope.subject.decode("utf-8", errors="ignore") if envelope.subject else ""
                        body = extract_body(mail_bytes)

                        client.set_flags([uid], [b"\\Seen"])

                        if not body:
                            print(f"[mail] 邮件正文为空，跳过 uid={uid}")
                            continue

                        params, error = parse_body(body)
                        if error:
                            print(f"[mail] 格式错误，回复提示 uid={uid}")
                            send_reply(from_addr, subject, f"邮件格式有误，无法生成视频。\n\n{error}")
                            continue

                        t = threading.Thread(target=handle_email, args=(from_addr, subject, params), daemon=True)
                        t.start()

                    # IDLE 等待新邮件推送，超时后重新 search
                    client.idle()
                    responses = client.idle_check(timeout=60)
                    client.idle_done()
                    

        except Exception as e:
            print(f"[mail] 连接错误: {e}，10秒后重连...")
            time.sleep(10)


if __name__ == "__main__":
    listen()
