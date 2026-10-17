# paper-download-skill

批量下载论文的 Agent Skill：按论文标题命名、去重、断点续下，检查错配论文和缺正文的 PDF，并生成可重试的未下载清单。

下载后端使用 [ZTPchemistry/scihub-downloader](https://github.com/ZTPchemistry/scihub-downloader)，固定到 `8fc00071a65e6fe3dca6ca7689eb79813d9ed906`。公开全文补取也复用该项目的 PDF 下载函数。PDF 校验在本机通过 [PyMuPDF](https://pymupdf.readthedocs.io/) 执行。

## 安装 Skill

把仓库的 `paper-download/` 文件夹复制到你的 Agent skills 目录。Codex 可使用：

```bash
git clone https://github.com/Hsintao/paper-download-skill.git
mkdir -p ~/.codex/skills
cp -R paper-download-skill/paper-download ~/.codex/skills/paper-download
```

如果目标目录已存在，先检查现有版本，避免覆盖自定义改动。`SKILL.md` 也可供支持同类目录规范的其他 Agent 使用。

## 安装运行依赖

Python 3.10+，需要 Git。建议使用项目虚拟环境：

```bash
cd paper-download-skill
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

运行已复制的 skill 时，使用同一虚拟环境的 Python。无需把论文列表、PDF 或认证信息放进本仓库。

## 使用

在 Agent 中：

> 使用 $paper-download 下载这份 parsed_entries.json，文件名用论文标题，列出没有下载成功的论文。

或直接运行：

```bash
python paper-download/scripts/download_papers.py --input examples/papers.json --out downloads --dry-run
python paper-download/scripts/download_papers.py --input examples/papers.json --out downloads --backend direct
python paper-download/scripts/download_papers.py --input parsed_entries.json --out downloads --backend auto
```

输入支持数组或 `{"entries": [...]}`，每条包含 `title`，以及可选的 `doi`、`pdf_urls`、`authors`、`year`。`auto` 先使用 Sci-Hub，再尝试已提供的公开 PDF 地址；`direct` 只使用公开直链及 arXiv DOI；`scihub` 只用 Sci-Hub。来源选择沿用用户要求。

有效 PDF 在 `downloads/pdfs/`。查看 `report.csv` 和 `pending.csv` 获取逐条结果；给 `pending.json` 补入新来源后可直接重试：

```bash
python paper-download/scripts/download_papers.py --input downloads/pending.json --out downloads
```

已有有效 PDF 会跳过；已有同名但无效的文件会报告冲突，保留原文件。同一输出目录保留累计报告，重试失败清单不会丢失成功记录，不同批次的同名论文会区分文件归属。

校验会拒绝传输长度不符、标题不符和大量空白页的候选文件；标题提取排除不可见渲染文字。扫描件或标题版本差异可能需要人工确认。校验不是逐页内容完整性证明，也不保证每篇都能找到可用全文。

具体字段、退出码和失败原因见 [输入输出说明](paper-download/references/input-output.md)。

## 开发检查

```bash
python -m pip install -r requirements-dev.txt
ruff check .
python -m unittest discover -s tests -v
python -m compileall -q paper-download/scripts
```

测试使用本地 HTTP 服务与实际生成的 PDF，验证去重、续传、错配拒绝、空白正文、同名冲突和失败清单重试，不依赖外部镜像可用性。

本项目采用 MIT 许可；外部依赖保持各自许可。下载论文时请遵守其来源的使用条款和适用版权要求。
