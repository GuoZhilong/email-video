# -*- coding: utf-8 -*-
import base64
import json
import mimetypes
import os
import requests

BASE_URL = "https://ai.leihuo.netease.com"
OPENAI_BASE_URL = "https://ai.leihuo.netease.com/v1"
COOKIES_FILE = "cookies.txt"

cookie_value = open(COOKIES_FILE).read().strip()
headers = {"Cookie": f"QAWEB_SESS={cookie_value}"}
token_resp = requests.post(f"{BASE_URL}/webapi/ai_account/token", json={"need_prefix": False}, headers=headers)
token = token_resp.json().get("key_full", "")
print(f"token: {token[:20]}...")

prompt = """第一张图是一个表情包，我希望你把图片右侧的两个王者英雄头像去掉，替换成一个第二张图的木兰头像，同时把右侧下方的红色狂暴图标替换成第三张图的暴君归来图标，图片左侧的英雄头像不动
"""

image_paths = [
    "技高一筹.jpg",
    "木兰.jpg",
    "暴君.png",
]

files = []
for i, path in enumerate(image_paths):
    with open(path, "rb") as f:
        image_data = f.read()
    mime, _ = mimetypes.guess_type(path)
    if not mime or mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        mime = "image/png"
    ext = os.path.splitext(path)[1] or ".png"
    filename = f"image_{i}{ext}"
    files.append(("image", (filename, image_data, mime)))

data = {
    "model": "gpt-image-2",
    "prompt":  prompt,
    "n": "1",
}

print("发送图片编辑请求...")
resp = requests.post(
    f"{OPENAI_BASE_URL}/images/edits",
    headers={"Authorization": f"Bearer {token}"},
    files=files,
    data=data,
)
result = resp.json()
print(f"status: {resp.status_code}")

if "error" in result:
    print(f"错误: {result['error']}")
else:
    img_list = result.get("data", [])
    if img_list and img_list[0].get("b64_json"):
        with open("我想_edited.png", "wb") as f:
            f.write(base64.b64decode(img_list[0]["b64_json"]))
        usage = result.get("usage", {})
        print(f"成功！已保存为 我想_edited.png，tokens: {usage.get('total_tokens')}")
    else:
        print("响应中无图片数据")
        print(json.dumps(result, ensure_ascii=False, indent=2))
