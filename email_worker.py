import json
import logging
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

os.makedirs(LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, "email_worker.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def send_reply(to_addr, subject, body, attachment_path=None):
    msg = MIMEMultipart()
    msg["From"] = QQ_EMAIL
    msg["To"] = to_addr
    msg["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_path:
        ext = os.path.splitext(attachment_path)[1].lower()
        mime_main, mime_sub = ("image", "png") if ext == ".png" else ("video", "mp4")
        with open(attachment_path, "rb") as f:
            part = MIMEBase(mime_main, mime_sub)
            part.set_payload(f.read())
        encoders.encode_base64(part)
        filename = attachment_path.split("\\")[-1].split("/")[-1]
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(QQ_EMAIL, QQ_AUTH_CODE)
        server.sendmail(QQ_EMAIL, to_addr, msg.as_string())
    logger.info("已回复 to=%s subject=%s attachment=%s", to_addr, subject, attachment_path)


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


def handle_image_edit(from_addr, subject, params, images):
    prompt = params["prompt"]
    logger.info(
        "图片编辑请求 from=%s subject=%s images=%d prompt=%s",
        from_addr, subject, len(images), prompt,
    )

    if not images:
        send_reply(from_addr, subject, "图片编辑需要至少附带 1 张图片，请重新发送邮件并附上图片。")
        return

    send_reply(from_addr, subject,
               f"已收到图片编辑请求（{len(images)} 张图片），处理中，完成后会自动发送给您。\n\nPrompt: {prompt}")

    resp = requests.post(f"{API_BASE}/api/edit", json={"prompt": prompt, "images": images})
    try:
        resp_json = resp.json()
    except Exception:
        logger.error("图片编辑失败（响应非JSON, status=%s）: %s", resp.status_code, resp.text[:500])
        send_reply(from_addr, subject, f"图片编辑失败（服务器返回异常，状态码 {resp.status_code}）：\n{resp.text[:200]}")
        return
    if resp.status_code != 200 or resp_json.get("error"):
        logger.error("图片编辑失败: %s", resp.text)
        send_reply(from_addr, subject, f"图片编辑失败：{resp_json.get('error', resp.text)}")
        return

    filename = resp_json.get("filename")
    logger.info("图片编辑完成 filename=%s", filename)

    if filename and os.path.exists(filename):
        send_reply(from_addr, subject, "您的图片已编辑完成，请查收附件。", attachment_path=filename)
    else:
        send_reply(from_addr, subject, "图片编辑完成，但文件暂时无法获取，请稍后重试。")


def handle_video(from_addr, subject, params, images):
    prompt = params["prompt"]
    logger.info(
        "视频生成请求 from=%s subject=%s prompt=%s images=%d model=%s duration=%s resolution=%s",
        from_addr, subject, prompt, len(images),
        params.get("model", "-"), params.get("duration", "-"), params.get("resolution", "-"),
    )

    img_hint = f"（附带 {len(images)} 张参考图片）" if images else ""
    send_reply(from_addr, subject,
               f"已收到您的请求，视频生成中，完成后会自动发送给您。\n\nPrompt: {prompt}{img_hint}")

    payload = {k: v for k, v in params.items() if k not in ("type",)}
    if images:
        payload["images"] = images

    resp = requests.post(f"{API_BASE}/api/generate", json=payload)
    resp_json = resp.json()
    if resp.status_code != 200 or resp_json.get("error"):
        send_reply(from_addr, subject, f"视频生成提交失败: {resp.text}")
        return

    task_id = resp_json["task_id"]
    logger.info("任务已入队 task_id=%s from=%s", task_id, from_addr)

    while True:
        time.sleep(POLL_INTERVAL)
        history = requests.get(f"{API_BASE}/api/history").json()
        item = next(
            (i for i in history if i["task_id"] == task_id or i.get("pending_id") == task_id),
            None,
        )
        if not item:
            continue
        real_task_id = item["task_id"]
        status = item["status"]
        logger.info("轮询 task_id=%s status=%s progress=%s", real_task_id, status, item.get("progress", ""))
        if status == "SUCCESS" and item.get("filename"):
            filename = item.get("filename")
            quota_info = read_quota_info(real_task_id)
            send_reply(from_addr, subject, f"您的视频已生成完成，请查收附件。{quota_info}", attachment_path=filename)
            break
        elif status in ("FAILED", "CANCELLED"):
            send_reply(from_addr, subject, f"视频生成失败，请重新发送邮件重试。状态: {status}")
            break


def handle_email(from_addr, subject, params, images):
    task_type = params.get("type", "").strip().lower()
    if task_type == "image":
        handle_image_edit(from_addr, subject, params, images)
    else:
        handle_video(from_addr, subject, params, images)


VALID_KEYS = {"duration", "resolution", "model", "type"}
FORMAT_HELP = """邮件正文格式：

【视频生成】
duration: 时长，秒，如 8（可选）
resolution: 分辨率，如 1280x720（可选）
model: 模型名称（可选）

prompt:
视频描述内容（必填，支持多行）
---

【图片编辑】
type: image

prompt:
描述编辑内容，如：把第一张图的人物插入到第二张图场景中（必填）
---

说明：
- 图片编辑需要附带 1 张或多张图片附件
- prompt: 单独一行，之后的内容全部作为提示词
- 用 --- 标记结束，--- 之后的内容（签名、引用等）会被忽略"""


def _clean_body(text):
    """清理邮件正文中的 HTML 实体和不可见字符。"""
    import html
    import re
    # 解码 HTML 实体（&nbsp; &amp; 等）
    text = html.unescape(text)
    # \xa0 是 &nbsp; 解码后的字符，替换为普通空格
    text = text.replace("\xa0", " ")
    # 去掉其他零宽字符
    text = re.sub(r"[​‌‍﻿]", "", text)
    return text


def parse_body(body):
    """解析邮件正文。

    格式：其他参数（key: value）在前，prompt: 单独一行后跟多行内容，--- 标记结束。
    --- 之后的所有内容（引用、签名）全部忽略。
    """
    body = _clean_body(body)
    params = {}
    prompt_lines = []
    in_prompt = False

    for line in body.splitlines():
        stripped = line.strip()

        # 遇到 --- 结束标记，停止解析
        if stripped.startswith("---") or stripped.startswith("==="):
            break

        if in_prompt:
            # prompt 模式：收集所有行（包括空行，保留换行结构）
            prompt_lines.append(line)
            continue

        # 引用行忽略
        if stripped.startswith(">"):
            continue

        if not stripped:
            continue

        if ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key = key.strip().strip("\xa0").lower()
        value = value.strip().strip("\xa0")

        if key == "prompt":
            in_prompt = True
            # prompt: 后面如果直接有内容，作为第一行
            if value:
                prompt_lines.append(value)
        elif key in VALID_KEYS:
            if not value:
                return None, f"字段「{key}」的值不能为空\n\n{FORMAT_HELP}"
            params[key] = value
        # 其他 key 忽略（邮件客户端元数据等）

    prompt = "\n".join(prompt_lines).strip()
    if not prompt:
        return None, f"缺少必填字段 prompt\n\n{FORMAT_HELP}"

    params["prompt"] = prompt
    return params, None


def extract_body_and_images(mail_bytes):
    """返回 (body_text, images)，images 为 base64 data URI 列表。"""
    import base64 as _b64
    msg = message_from_bytes(mail_bytes)
    body = ""
    images = []

    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    from email.header import decode_header as _dh2

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            # filename 可能是 MIME encoded-word，需要解码才能正确取后缀
            raw_filename = part.get_filename() or ""
            if raw_filename:
                decoded_parts = _dh2(raw_filename)
                filename = "".join(
                    p.decode(enc or "utf-8", errors="ignore") if isinstance(p, bytes) else p
                    for p, enc in decoded_parts
                )
            else:
                filename = ""
            ext = os.path.splitext(filename)[1].lower() if filename else ""

            logger.debug("邮件 part ct=%s cd=%s filename=%s", ct, cd.strip(), filename)

            if ct == "text/plain" and not body:
                body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="ignore"
                )
            elif ct.startswith("image/") or (
                ct in ("application/octet-stream", "application/unknown") and ext in IMAGE_EXTS
            ) or (filename and ext in IMAGE_EXTS):
                data = part.get_payload(decode=True)
                if data:
                    # 用文件名推断真实 mime type
                    mime = ct if ct.startswith("image/") else f"image/{ext.lstrip('.') or 'jpeg'}"
                    b64 = _b64.b64encode(data).decode()
                    images.append(f"data:{mime};base64,{b64}")
                    logger.info("提取图片附件 filename=%s mime=%s size=%d bytes", filename, mime, len(data))
    else:
        body = msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="ignore"
        )

    return body.strip(), images


def listen():
    logger.info("开始监听 %s ...", QQ_EMAIL)
    while True:
        try:
            processed_uids = set()
            with IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
                client.login(QQ_EMAIL, QQ_AUTH_CODE)
                client.select_folder("INBOX")
                logger.info("已连接，等待新邮件 (IDLE)...")

                while True:
                    uids = client.search(["UNSEEN"])
                    for uid in uids:
                        if uid in processed_uids:
                            continue
                        processed_uids.add(uid)

                        raw = client.fetch([uid], ["RFC822", "ENVELOPE"])
                        envelope = raw[uid][b"ENVELOPE"]
                        mail_bytes = raw[uid][b"RFC822"]

                        from_addr = envelope.from_[0].mailbox.decode() + "@" + envelope.from_[0].host.decode()
                        # subject 可能是 MIME encoded-word 格式，需要解码
                        from email.header import decode_header as _dh
                        raw_subj = envelope.subject or b""
                        if isinstance(raw_subj, bytes):
                            parts = _dh(raw_subj.decode("ascii", errors="replace"))
                            subject = "".join(
                                p.decode(enc or "utf-8", errors="ignore") if isinstance(p, bytes) else p
                                for p, enc in parts
                            )
                        else:
                            subject = raw_subj
                        body, images = extract_body_and_images(mail_bytes)

                        logger.info(
                            "收到邮件 uid=%s from=%s subject=%s body_len=%d images=%d",
                            uid, from_addr, subject, len(body), len(images),
                        )
                        logger.debug("邮件正文:\n%s", body)

                        client.set_flags([uid], [b"\\Seen"])

                        if not body:
                            logger.info("邮件正文为空，跳过 uid=%s", uid)
                            continue

                        params, error = parse_body(body)
                        if error:
                            logger.warning("格式错误 uid=%s from=%s error=%s", uid, from_addr, error.splitlines()[0])
                            send_reply(from_addr, subject, f"邮件格式有误，无法处理请求。\n\n{error}")
                            continue

                        t = threading.Thread(target=handle_email, args=(from_addr, subject, params, images), daemon=True)
                        t.start()

                    # IDLE 等待新邮件推送，超时后重新 search
                    client.idle()
                    client.idle_check(timeout=10)
                    client.idle_done()

        except KeyboardInterrupt:
            logger.info("收到中断信号，退出监听。")
            return
        except Exception as e:
            logger.error("连接错误: %s，10秒后重连...", e)
            try:
                time.sleep(10)
            except KeyboardInterrupt:
                logger.info("收到中断信号，退出监听。")
                return


if __name__ == "__main__":
    listen()
