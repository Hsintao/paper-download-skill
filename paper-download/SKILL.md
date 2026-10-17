---
name: paper-download
description: "Use when asked to download research papers from DOI/title lists or parsed_entries.json, name PDFs by paper title, resume a batch, or retry missing papers. Uses scihub-downloader and supplied public PDF sources, checks visible titles and missing body pages, and produces download and pending reports."
---

# 论文批量下载

把论文列表下载为按标题命名的 PDF，保留逐条记录、去重关系、来源和失败原因。下载后做保守校验；HTTP 200、`%PDF` 文件头和文件内置标题都不足以证明拿到了正确全文。

## 执行

1. 读取用户给出的列表，确认输出目录和指定下载方式。输入支持 JSON 数组以及 `{"entries": [...]}`。字段说明见 [references/input-output.md](references/input-output.md)。用户已有选择和授权时直接沿用。
2. 缺 DOI 或全文链接时，按标题查询论文的会议官网、作者主页、arXiv 或机构仓储。核对标题、作者和年份；不要把第一条模糊搜索结果直接当成目标论文。把确认后的 DOI 和 `pdf_urls` 写入新的工作 JSON，保留原始输入。出版社 DOI 与预印本可以不同，文件名仍使用输入标题。
3. 将本技能目录中的 `requirements.txt` 安装到项目虚拟环境：`python -m pip install -r "<skill-dir>/requirements.txt"`。脚本依赖的 `scihub-downloader` 固定到已验证的提交；已有 checkout 时可通过 `--scihub-repo` 使用。
4. 先预览文件名，再下载。将 `<skill-dir>` 替换为本文件所在目录，路径含空格时加引号：

   ```bash
   python "<skill-dir>/scripts/download_papers.py" --input papers.json --out downloads --dry-run
   python "<skill-dir>/scripts/download_papers.py" --input papers.json --out downloads --backend auto
   ```

   `auto` 使用用户选定的 Sci-Hub 下载流程，并补取输入中的公开 PDF 链接。`direct` 只用这些链接及 arXiv DOI；`scihub` 只走 Sci-Hub。用户限制来源时选择相应模式。脚本不会自行登录、订阅、购买或解除验证码。
5. 原命令重跑会重新检查并跳过已有有效 PDF。`pending.json` 可直接作为 `--input` 重试；先补入新核实的来源再重试，避免反复请求相同失败地址。保留所有来源尝试后停止；没有可用全文时如实说明。

## 校验和交付

- PDF 在本机校验，检查结构、第一页可见标题以及多数正文页为空白的异常。元数据不覆盖可见标题不符的结果。
- 校验未通过的下载留在 `rejected/`，不算成功；扫描件或无法提取标题的文件标为待核实。必要时渲染首页人工核对，未经核实不要声称拿到了全文。
- 已有同名文件校验不通过时报告 `existing_file_conflict` 并保留文件。不要以覆盖旧文件的方式解决冲突。
- 输出 `pdfs/`、`report.json/.csv`、`pending.json/.csv`、`summary.json` 和 `events.jsonl`。分别报告原始条目数、去重论文数、成功 PDF 数及未下载数，不用工具的“已保存”代替校验结果。
- 把 PDF 目录和未下载清单的路径交给用户。用户只要求下载时，不把论文或列表发布到 GitHub，也不修改原始文献数据。
