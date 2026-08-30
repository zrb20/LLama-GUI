/* 模型库管理 UI：列表 / 详情 / 删除（两步确认）/ 定位文件夹。 */
(function () {
    "use strict";

    let deps = {};
    let items = [];          // [{name, size_mb}]
    let selected = "";       // 当前选中的 rel path
    let expandedOnce = false;

    function configure(nextDeps) {
        deps = Object.assign({}, deps, nextDeps || {});
    }

    function require(name) {
        const value = deps[name];
        if (typeof value !== "function") throw new Error(`model manager dependency missing: ${name}`);
        return value;
    }

    function showStatus(type, message) {
        const el = document.getElementById("mm-status");
        if (!el) return;
        el.className = "hf-download-status" + (type ? " " + type : "");
        el.textContent = message || "";
    }

    function fmtGB(sizeMb) {
        if (sizeMb >= 1024) return (sizeMb / 1024).toFixed(2) + " GB";
        return sizeMb.toFixed(1) + " MB";
    }

    function pickQuant(name) {
        const tags = ["IQ4_XS", "IQ4_NL", "UD-IQ2_XXS", "Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M",
            "Q4_K_S", "Q5_K_S", "Q3_K_M", "Q3_K_L", "Q2_K", "BF16", "F16", "F32", "Q8", "Q6", "Q5", "Q4"];
        const upper = name.toUpperCase();
        for (const t of tags) if (upper.includes(t)) return t;
        return "";
    }

    async function loadList() {
        const fetchJson = require("fetchJson");
        try {
            const models = await fetchJson("/api/models");
            items = Array.isArray(models) ? models.filter((m) => m && m.name) : [];
            renderList();
            if (!items.length) showStatus("info", "模型文件夹里还没有模型——可从快速启动页下载。");
            else showStatus("", "");
        } catch (e) {
            showStatus("error", "模型列表加载失败: " + e.message);
        }
    }

    function renderList() {
        const list = document.getElementById("mm-list");
        if (!list) return;
        list.textContent = "";
        for (const m of items) {
            const row = document.createElement("div");
            row.className = "mm-item" + (m.name === selected ? " active" : "");

            const main = document.createElement("div");
            main.className = "mm-item-main";
            const nameEl = document.createElement("div");
            nameEl.className = "mm-item-name";
            nameEl.textContent = m.name;
            const meta = document.createElement("div");
            meta.className = "mm-item-meta";
            const sizeEl = document.createElement("span");
            sizeEl.textContent = fmtGB(Number(m.size_mb) || 0);
            meta.appendChild(sizeEl);
            const quant = pickQuant(m.name);
            if (quant) {
                const badge = document.createElement("span");
                badge.className = "mm-badge badge-quant";
                badge.textContent = quant;
                meta.appendChild(badge);
            }
            main.appendChild(nameEl);
            main.appendChild(meta);

            const actions = document.createElement("div");
            actions.className = "mm-item-actions";
            const infoBtn = document.createElement("button");
            infoBtn.className = "btn btn-sm";
            infoBtn.type = "button";
            infoBtn.textContent = "信息";
            infoBtn.addEventListener("click", (ev) => {
                ev.stopPropagation();
                showInfo(m.name);
            });
            const revealBtn = document.createElement("button");
            revealBtn.className = "btn btn-sm btn-ghost";
            revealBtn.type = "button";
            revealBtn.textContent = "定位";
            revealBtn.addEventListener("click", (ev) => {
                ev.stopPropagation();
                reveal(m.name);
            });
            const delBtn = document.createElement("button");
            delBtn.className = "btn btn-sm btn-danger";
            delBtn.type = "button";
            delBtn.textContent = "删除";
            delBtn.addEventListener("click", (ev) => {
                ev.stopPropagation();
                confirmDelete(m.name);
            });
            actions.appendChild(infoBtn);
            actions.appendChild(revealBtn);
            actions.appendChild(delBtn);

            row.appendChild(main);
            row.appendChild(actions);
            row.addEventListener("click", () => showInfo(m.name));
            list.appendChild(row);
        }
    }

    async function showInfo(name) {
        const fetchJson = require("fetchJson");
        selected = name;
        renderList();
        const panel = document.getElementById("mm-detail");
        if (!panel) return;
        panel.classList.remove("hidden");
        panel.innerHTML = "";
        const loading = document.createElement("p");
        loading.textContent = "读取模型信息…";
        panel.appendChild(loading);
        try {
            const info = await fetchJson("/api/model-manager/info/" + encodeURIComponent(name));
            panel.textContent = "";
            const h = document.createElement("h4");
            h.textContent = name;
            panel.appendChild(h);

            const grid = document.createElement("dl");
            grid.className = "mm-detail-grid";
            const addRow = (key, value) => {
                if (value === undefined || value === null || value === "") return;
                const dt = document.createElement("dt");
                dt.textContent = key;
                const dd = document.createElement("dd");
                dd.textContent = String(value);
                grid.appendChild(dt);
                grid.appendChild(dd);
            };
            addRow("大小", info.size_gb + " GB");
            const gguf = info.gguf || {};
            addRow("架构", gguf.architecture);
            addRow("量化", gguf.quantization);
            addRow("参数规模", gguf.size_label);
            addRow("上下文长度", gguf.context_length);
            addRow("层数", gguf.block_count);
            addRow("张量数", gguf.tensor_count);
            addRow("专家数 (MoE)", gguf.expert_count ? `${gguf.expert_count}（用 ${gguf.expert_used || "?"}）` : null);
            addRow("GGUF 版本", gguf.gguf_version);
            addRow("完整路径", info.absolute_path);
            if (info.gguf_error) addRow("元数据", info.gguf_error);
            panel.appendChild(grid);

            const actions = document.createElement("div");
            actions.className = "mm-detail-actions";
            const reveal = document.createElement("button");
            reveal.className = "btn btn-sm btn-ghost";
            reveal.type = "button";
            reveal.textContent = "打开所在文件夹";
            reveal.addEventListener("click", () => reveal(name));
            const del = document.createElement("button");
            del.className = "btn btn-sm btn-danger";
            del.type = "button";
            del.textContent = "删除此模型";
            del.addEventListener("click", () => confirmDelete(name));
            actions.appendChild(reveal);
            actions.appendChild(del);
            panel.appendChild(actions);
        } catch (e) {
            panel.textContent = "";
            const err = document.createElement("p");
            err.textContent = "读取信息失败: " + e.message;
            panel.appendChild(err);
        }
    }

    async function reveal(name) {
        const fetchJson = require("fetchJson");
        try {
            await fetchJson("/api/model-manager/reveal", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: name }),
            });
            showStatus("success", "已在资源管理器中打开。");
        } catch (e) {
            showStatus("error", "打开文件夹失败: " + e.message);
        }
    }

    async function confirmDelete(name) {
        const fetchJson = require("fetchJson");
        const confirmAction = require("confirmAction");
        try {
            const stat = await fetchJson("/api/model-manager/stat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: name }),
            });
            const sizeText = fmtGB((Number(stat.size_bytes) || 0) / 1048576);
            const what = Number(stat.files) > 1
                ? `${name} 及其中全部 ${stat.files} 个文件`
                : name;
            const ok = await confirmAction(
                "删除模型",
                `将删除 ${what}（共 ${sizeText}）。此操作不可撤销，确定删除吗？`
            );
            if (!ok) return;
            await fetchJson("/api/model-manager/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: name }),
            });
            showStatus("success", `已删除 ${name}（${sizeText}）。`);
            if (selected === name) {
                selected = "";
                const panel = document.getElementById("mm-detail");
                if (panel) panel.classList.add("hidden");
            }
            await loadList();
            if (typeof deps.refreshModels === "function") await deps.refreshModels();
        } catch (e) {
            showStatus("error", "删除失败: " + e.message);
        }
    }

    function init() {
        const panel = document.getElementById("model-manager-panel");
        if (!panel) return;
        panel.addEventListener("toggle", () => {
            if (panel.open && !expandedOnce) {
                expandedOnce = true;
                loadList();
            }
        });
    }

    window.LlamaGui = window.LlamaGui || {};
    window.LlamaGui.modelManagerUi = {
        configure,
        init,
        loadList,
        showInfo,
        confirmDelete,
    };
})();
