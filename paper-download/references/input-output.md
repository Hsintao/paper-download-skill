# 输入、输出与重试

## JSON 输入

支持数组，或解析工具产生的 `{"entries": [...]}`。每条必须有非空 `title`。

```json
{
  "entries": [
    {
      "title": "Attention Is All You Need",
      "doi": "10.48550/arXiv.1706.03762",
      "year": 2017,
      "pdf_urls": ["https://arxiv.org/pdf/1706.03762"]
    }
  ]
}
```

- `doi` 可省略，也可使用 DOI URL；`resolved_doi` 是另一个受支持的补充字段。脚本只规范化，不自动验证 DOI 元数据。
- `pdf_urls` 为已经核实的 HTTP(S) PDF 地址数组，按顺序尝试。HTML 论文介绍页不等于 PDF 直链；拿到 HTML 时报告非 PDF。
- `authors`、`year`、`index` 等字段会保留在报告和失败清单里。
- 带 DOI 的条目按规范化 DOI 去重；不带 DOI 的按规范化标题和年份去重。不同 DOI 不会因为标题相似而合并。
- 非法路径字符替换为下划线，过长标题按 UTF-8 字节截断。不同论文产生同名文件时，加确定性哈希后缀。

## 命令

```bash
python paper-download/scripts/download_papers.py --input papers.json --out downloads --backend auto
python paper-download/scripts/download_papers.py --input papers.json --out downloads --backend direct --concurrency 2 --timeout 30
python paper-download/scripts/download_papers.py --input downloads/pending.json --out downloads --backend auto
python paper-download/scripts/download_papers.py --input papers.json --out downloads --scihub-repo /path/to/scihub-downloader
```

`--concurrency` 为 1–8，默认 4；`--timeout` 为单次 HTTP 请求的超时，默认 30 秒，范围为大于 0 且不超过 120 秒。一篇论文可能依次尝试多个镜像和来源，总耗时可超过单次请求超时。

`--dry-run` 仅输出去重后的计划，不联网、不创建输出目录。退出码：`0` 全部成功或预览成功；`1` 仍有未下载条目；`2` 输入、依赖或文件操作错误；`130` 用户中断。

## 输出

| 文件 | 内容 |
| --- | --- |
| `pdfs/` | 校验通过、按标题命名的 PDF |
| `rejected/` | 已下载但标题不符、缺正文或结构异常的候选文件 |
| `report.json` / `report.csv` | 原始条目逐条结果、重复关系、来源和校验数据 |
| `pending.json` / `pending.csv` | 未下载的条目和原因；JSON 可直接重新作为输入 |
| `summary.json` | 原始条目、去重论文、成功 PDF 和未下载条目的数量 |
| `events.jsonl` | 逐次来源尝试；保留具体 HTTP/下载错误 |
| `.staging/` | 下载临时文件；未经校验不进入 `pdfs/` |

运行中每处理完一篇更新 JSON 检查点，结束时写 CSV。同一输出目录保留累计报告和文件名归属；只重试 `pending.json` 不会删除之前成功的记录。不同输入批次中的同名、不同 DOI 论文会使用不同文件名。需要单独统计一批论文时，使用新的输出目录。

重跑会校验已有文件并跳过有效文件；未在本次输入中的旧失败条目保留在清单中，不自动发起新下载。同名无效文件不会被覆盖。下载时核对服务器声明的 `Content-Length`（如提供），拒绝传输长度不符的响应。

## 常见失败

- `title_mismatch`：可见标题不匹配，可能是错配论文或标题版本不同，需要核对。
- `title_unverifiable`：无法提取可见标题，常见于扫描件；需要人工核对。
- `missing_body_pages`：至少一半的页面没有正文、图片或矢量内容，可能只有摘要预览。
- `bad_pdf` / `invalid_pdf`：HTML、错误页或损坏 PDF。
- `network_error` / `captcha` / `not_found` / `no_pdf`：下载器返回的具体来源失败。
- `existing_file_conflict`：输出目录已存在同名但未通过校验的文件，原文件保持不变。
- `no_usable_source`：当前模式没有可尝试的 DOI 或 PDF 来源。

标题匹配和空白页检查是保守的自动筛查，不构成逐页内容完整性证明。不要把待核实文件直接计入成功数。
