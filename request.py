import time
import requests

base_url = "https://ai.leihuo.netease.com"
cookie_value = open("cookies.txt").read().strip()
headers = {"Cookie": f"QAWEB_SESS={cookie_value}"}
data = {
    "need_prefix": False,
}

resp = requests.post(url=f"{base_url}/webapi/ai_account/token", json=data, headers=headers)
print(resp.text)

token = resp.json().get("key_full", "")

openai_base_url = "https://ai.leihuo.netease.com/v1"
openai_headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}
video_data = {
    "model": "doubao-seedance-2-0-fast-260128",
    "prompt": "A beautiful sunset over the ocean",
}

video_resp = requests.post(
    url=f"{openai_base_url}/video/generations",
    json=video_data,
    headers=openai_headers,
)
print(video_resp.text)

task_id = video_resp.json().get("task_id", "")
if not task_id:
    print("No task_id returned, exiting.")
    exit(1)

print(f"\nPolling task {task_id} ...")
while True:
    poll_resp = requests.get(
        url=f"{openai_base_url}/video/generations/{task_id}",
        headers=openai_headers,
    )
    result = poll_resp.json()
    data = result.get("data", {})
    status = data.get("status", "")
    progress = data.get("progress", 0)
    print(f"  status={status} progress={progress}")
    if status in ("SUCCESS", "FAILED", "CANCELLED"):
        print(result)
        if status == "SUCCESS":
            video_url = data.get("data", {}).get("content", {}).get("video_url", "")
            if video_url:
                filename = f"{task_id}.mp4"
                download_resp = requests.get(video_url, stream=True)
                with open(filename, "wb") as f:
                    for chunk in download_resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                print(f"Video saved to {filename}")
        break
    time.sleep(5)
