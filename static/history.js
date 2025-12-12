async function loadData() {
    let type = document.getElementById("type").value;
    let limit = document.getElementById("limit").value;
    let url = (type === "latest") ? `/latest?limit=${limit}` : `/${type}?limit=${limit}`;

    try {
        const res = await fetch(window.location.origin + url);
        const data = await res.json();
        const report = document.getElementById("report");
        report.innerHTML = "";

        // ===================== requests 模式 =====================
        if (type === "requests") {
            const list = data.data || [];
            if (list.length === 0) {
                report.innerHTML = `<div class="card"><b>无 Request 数据</b></div>`;
                return;
            }
            list.forEach(item => {
                const ts = new Date(item.timestamp).toLocaleString();
                report.innerHTML += `
                    <div class="card">
                        <div class="title">📌 Request 投喂内容</div>
                        <div class="time">时间：${ts}</div>
                        <div class="section"><pre>${item.request}</pre></div>
                    </div>
                `;
            });
            return;
        }

        // ===================== responses 模式 =====================
        if (type === "responses") {
            const list = data.data || [];
            if (list.length === 0) {
                report.innerHTML = `<div class="card"><b>无 Response 数据</b></div>`;
                return;
            }
            list.forEach(resItem => renderResponseCard(null, resItem));
            bindButtons();
            return;
        }

        // ===================== latest 模式（多条 Request + Response） =====================
        if (type === "latest") {
            const reqs = data.request || [];
            const ress = data.response || [];

            if (reqs.length === 0 || ress.length === 0) {
                report.innerHTML = `<div class="card"><b>无最新记录</b></div>`;
                return;
            }

            for (let i = 0; i < ress.length; i++) {
                const req = reqs[i] || null;
                const resItem = ress[i];
                renderResponseCard(req, resItem);
            }
            bindButtons();
            return;
        }

    } catch (err) {
        document.getElementById("report").innerHTML =
            `<div class="card"><b>加载失败：</b><br>${err}</div>`;
    }
}

/* =========================================================
   ✨ 解析 XML 标签 <reasoning> 和 <decision>
========================================================= */
function extractTagContent(raw, tag) {
    const regex = new RegExp(`<${tag}>([\\s\\S]*?)<\/${tag}>`, "i");
    const match = raw.match(regex);
    return match ? match[1].trim() : "";
}

/* =========================================================
   🔧 渲染单条 Request + Response 卡片
========================================================= */
function renderResponseCard(req, res) {
    const report = document.getElementById("report");
    const ts = new Date(res.timestamp).toLocaleString();

    let deepseek;
    try { deepseek = JSON.parse(res.response_raw); } catch {}

    let rawText =
        deepseek?.choices?.[0]?.message?.content ||
        deepseek?.message?.content ||
        res.response_raw ||
        "";

    // ---- 新解析逻辑：从 XML 标签获取内容 ----
    const reasoning = extractTagContent(rawText, "reasoning");
    const decisionStr = extractTagContent(rawText, "decision");

    let signals = null;
    try { signals = JSON.parse(decisionStr); } catch {}

    // 降级兼容旧格式
		let textPart = "";

		if (reasoning) {
				textPart = reasoning;
		} else {
				textPart = "当前用户已设置禁止输出思维链";
		}

    let html = `
        <div class="card">
            <div class="title">🧠 DeepSeek 分析结果</div>
            <div class="time">时间：${ts}</div>
    `;

    // 展示 Request（如果有）
    if (req?.request) {
        html += `
            <div class="section collapsible">
                <button class="toggle">📌 展开/折叠投喂内容</button>
                <div class="content" style="display:none;">
                    <pre>${req.request}</pre>
                </div>
            </div>
        `;
    }

    // 展示推理内容
    if (textPart) {
        html += `
            <div class="section collapsible">
                <button class="toggle">📌 展开/折叠分析内容</button>
                <div class="content" style="display:block;">
                    <pre>${textPart}</pre>
                </div>
            </div>
        `;
    }

    // 展示交易信号 JSON
    if (signals) {
        const pretty = JSON.stringify(signals, null, 2);
        const encoded = encodeURIComponent(pretty);
        html += `
            <div class="section collapsible">
                <button class="toggle">🚨 展开/折叠 AI 最终交易信号</button>
                <button class="copy" data-json="${encoded}">📋 复制 JSON</button>
                <div class="content" style="display:block;">
                    <pre class="json">${syntaxHighlight(pretty)}</pre>
                </div>
            </div>
        `;
    }

    html += `</div>`;
    report.innerHTML += html;
}

/* =========================================================
   折叠 + 复制绑定
========================================================= */
function bindButtons() {
    // 折叠
    document.querySelectorAll(".section.collapsible .toggle").forEach(btn => {
        btn.onclick = () => {
            const content = btn.closest(".section.collapsible").querySelector(".content");
            content.style.display = (content.style.display === "none" || !content.style.display)
                ? "block"
                : "none";
        };
    });

    // 复制
    document.querySelectorAll(".section.collapsible .copy").forEach(btn => {
        btn.onclick = () => {
            const raw = decodeURIComponent(btn.getAttribute("data-json"));
            if (navigator.clipboard?.writeText) {
                navigator.clipboard.writeText(raw);
            } else {
                const ta = document.createElement("textarea");
                ta.value = raw;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand("copy");
                document.body.removeChild(ta);
            }
            alert("📋 JSON 已复制");
        };
    });
}

/* =========================================================
   JSON 代码高亮
========================================================= */
function syntaxHighlight(json) {
    json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return json.replace(
        /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+\.\d+|-?\d+)/g,
        match => {
            let cls = "number";
            if (/^"/.test(match)) cls = /:$/.test(match) ? "key" : "string";
            else if (/true|false/.test(match)) cls = "boolean";
            else if (/null/.test(match)) cls = "null";
            return `<span class="${cls}">${match}</span>`;
        }
    );
}
