from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from typing import Optional
import json
import uvicorn
from database import redis_client
from fastapi.staticfiles import StaticFiles

KEY_REQ = "deepseek_analysis_request_history"
KEY_RES = "deepseek_analysis_response_history"

app = FastAPI(title="DeepSeek Analysis History API")

def _read_list(key: str, limit: int):
    items = redis_client.lrange(key, 0, limit - 1)
    result = []
    for item in items:
        try:
            result.append(json.loads(item))
        except Exception:
            result.append({"raw": item})
    return result


@app.get("/requests")
async def get_requests(limit: Optional[int] = Query(50, ge=1, le=500)):
    return {"count": limit, "data": _read_list(KEY_REQ, limit)}


@app.get("/responses")
async def get_responses(limit: Optional[int] = Query(50, ge=1, le=500)):
    return {"count": limit, "data": _read_list(KEY_RES, limit)}

@app.get("/latest")
async def get_latest_pair(limit: int = Query(1, ge=1, le=300)):
    reqs = redis_client.lrange(KEY_REQ, 0, limit - 1)
    ress = redis_client.lrange(KEY_RES, 0, limit - 1)

    def safe(x):
        if not x:
            return None
        try:
            return json.loads(x)
        except:
            return {"raw": x}

    return {
        "request": [safe(r) for r in reqs],
        "response": [safe(r) for r in ress]
    }

app.mount("/static", StaticFiles(directory="static"), name="static")
# ----------------- HTML 页面 -----------------
html_page = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>DeepSeek Analysis History</title>
<style>
body {
    background: #0b0c10;
    color: #e8e8e8;
    font-family: "Inter", "Consolas", sans-serif;
    margin: 0;
    padding: 20px;
}

.card {
    background: #111319;
    border: 1px solid #1d2330;
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 22px;
    box-shadow: 0 0 18px rgba(0, 0, 0, 0.45);
}

.card .title {
    font-size: 18px;
    font-weight: bold;
    margin-bottom: 6px;
    color: #5ab2ff;
}

.card .time {
    font-size: 13px;
    margin-bottom: 10px;
    color: #b5b5b5;
}

.card .section {
    background: #181c27;
    border-radius: 8px;
    padding: 12px;
    margin-top: 12px;
    overflow-x: auto;
}

/* JSON 折叠区域 */
.section.collapsible .toggle,
.section.collapsible .copy {
    padding: 6px 12px;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    margin-right: 6px;
    font-size: 14px;
    margin-bottom: 8px;
}

.section.collapsible .toggle {
    background: #2b78ff;
    color: white;
}
.section.collapsible .copy {
    background: #00c853;
    color: white;
}

.section.collapsible .toggle:hover {
    background: #1f62d3;
}
.section.collapsible .copy:hover {
    background: #009842;
}

/* 🧠 分析内容换行（reasoning 区域） */
pre:not(.json) {
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.55;
    font-size: 15px;
    max-height: 360px;
    overflow-y: auto;
}

/* 🔥 JSON 高亮（保持缩进格式，不换行） */
pre.json {
    background: #0f1118;
    padding: 14px;
    border-radius: 8px;
    font-family: Consolas, monospace;
    font-size: 14px;
    line-height: 1.45;
    white-space: pre;
    overflow-x: auto;
}

pre.json .key { color: #ffca5f; }
pre.json .string { color: #7cd6ff; }
pre.json .number { color: #9aff6b; }
pre.json .boolean { color: #ff9e52; }
pre.json .null { color: #ff6363; }

/* 顶部区域选择框美化 */
.controls {
    margin-bottom: 18px;
}

.controls select, .controls input {
    background: #10131a;
    color: white;
    border: 1px solid #2a3143;
    border-radius: 6px;
    padding: 6px 10px;
    margin-right: 8px;
}

.controls button {
    background: #2b78ff;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    cursor: pointer;
}
.controls button:hover {
    background: #1b5ecd;
}

/* 滚动条美化 */
::-webkit-scrollbar {
    width: 8px;
    height: 8px;
}
::-webkit-scrollbar-track {
    background: #0f1118;
}
::-webkit-scrollbar-thumb {
    background: #313748;
    border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
    background: #495168;
}
</style>

</head>
<body>

<div class="controls">
    <label>类型：</label>
    <select id="type">
        <option value="responses">响应</option>
        <option value="requests">请求</option>
        <option value="latest">最新一次(Request+Response)</option>
    </select>

    <label style="margin-left:10px;">条数：</label>
    <input id="limit" type="number" value="20" min="1" max="300" style="width:60px;">
    <button onclick="loadData()">刷新</button>
</div>

<!-- 🔥 页面核心展示区域 -->
<div id="report"></div>

<script src="/static/history.js"></script>
<script>
    window.onload = () => loadData();
</script>
</body>

</html>
"""

@app.get("/", response_class=HTMLResponse)
async def history_page():
    return HTMLResponse(html_page)
# --------------------------------------------------


if __name__ == "__main__":
    import os
    filename = os.path.basename(__file__).replace(".py", "")
    uvicorn.run(
        f"{filename}:app",
        host="0.0.0.0",
        port=8600,
        reload=True
    )
