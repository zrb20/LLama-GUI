# Llama GUI（中文分支 · zrb20 fork）

> 本文件说明 **zrb20 fork** 在**上游（thomas9120/LLama-GUI）基础上新增的功能**。
> 上游的英文说明见 [`README.md`](README.md)。
>
> 本 fork 遵循 GPL-3.0，仅维护在 fork 分支（`zh-cn`），不向上游提交改动。

---

## ✨ 本 fork 新增功能

### 1. 全中文界面（zh-cn 分支工作重点）
上游为英文界面，本 fork 的 `zh-cn` 分支完成**全界面中文化**：
- 侧栏 / 卡片 / 快速启动页 / 配置页参数说明 / 预设与采样器页面 / 导航项 / 徽章 等全部中文化
- 下载链路动态消息中文化（前端 + HF/魔搭服务 + 测试断言同步）
- 中文文案改动均同步更新自动化测试断言（`flag_sync_smoke.cjs` / `hf_download_ui_unit.cjs`），保证中文化不破坏测试

### 2. 魔搭（ModelScope）下载源 + 多线程分块下载
- 新增 **ModelScope 魔搭**模型源，可在 Hugging Face / 魔搭之间切换下载
- 魔搭下载采用**多线程 Range 分块并行下载**，突破单线程限速
- 与 Hugging Face 下载共用一套**并行分块下载引擎**（`backend/services/http_chunks.py`）

### 3. 双轨并行下载 + 双进度条
- 主模型与 `mmproj`（视觉投影器）**并行下载**，界面显示**双进度条 + 各自实时速率**
- 修复双轨进度偏移、已完成轨道速率显示、已下载文件检测等细节问题
- 下载中断可恢复，重复下载自动检测已存在文件

### 4. 模型库管理面板
- 扫描模型目录内全部 GGUF，卡片展示名称/大小/**量化徽章**（从文件名识别 Q4_K_M / Q8_0 等）
- 点卡片查看 **GGUF 头元数据**（架构 / 量化 / 参数规模 / 上下文长度 / 层数 / MoE 专家数 / 张量数）——使用**纯 Python 标准库手写 GGUF 头解析器**，只读文件头，不加载张量
- 一键定位模型文件夹、两步确认删除模型（被 llama-server 占用时返回友好错误）
- 路径安全：所有操作限制在模型根目录内，拒绝 `../` 逃逸与绝对路径

### 5. Bug 修复（选列）
- **修复内存估算漏算 MTP 投机解码显存**：补全 `_memory_estimate_args` 白名单（12 项参数，含 `--spec-type draft-mtp`、`--spec-draft-n-max`、`-md` 草稿模型路径、`--mmproj` 等），新增单元测试
- 修复模型信息接口 URL 编码路径解码（`%2F`）
- 修复模型库面板样式复用、双进度条填充宽度等问题

---

## 🧪 质量保障

- 后端 **642 项单元测试全绿**（含新增的 MTP 参数转发、GGUF 解析测试）
- 前后端改动均有对应测试：`python -m unittest discover tests` + `npm run test:flag-definitions` + `flag_sync_smoke.cjs` 冒烟
- 变更同步更新 `docs/changelog.md` 与 `docs/` 路由文档（文档与测试双向强制同步）

---

## 🚀 快速开始（与上游一致）

```bash
# Windows / macOS / Linux，Python 3.9+
python -m venv .venv
# Windows: .venv\Scripts\activate   |  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python server.py
# 浏览器打开 http://127.0.0.1:8080
```

详细使用说明请见上游 [`README.md`](README.md)。

---

## 📄 License

GPL-3.0（与上游一致）。本 fork 仅用于个人学习与使用，欢迎 fork。
