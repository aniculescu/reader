# reader

Tools for building study editions of Rudolf Steiner's lectures (.docx for print, EPUB)
from rsarchive.org and a local catalog. The toolkit is the `rsarchive` package; see
[`rsarchive/README.md`](rsarchive/README.md). Scanned books (archive.org scans, OCR,
reconciliation, EPUB/PDF) go through the `ocrbook` package; see
[`ocrbook/README.md`](ocrbook/README.md). Working conventions are in
[`docs/agent-notes.md`](docs/agent-notes.md).

## Setup

The project is a [uv](https://docs.astral.sh/uv/) project pinned to Python 3.12
(`.python-version`); macOS's own `python3` is older and isn't used.

```sh
make setup                 # uv sync --all-extras: .venv with every optional dependency
uv run rsarchive --help    # the command-line toolkit
make check                 # tests + lint before committing (make types for pyright)
```

The core needs only `beautifulsoup4`. The other dependencies are optional extras in
`pyproject.toml`, imported only by the commands that use them:

| Extra | For |
|---|---|
| `pdf` | `contents-pages`, `docx --toc-pages` |
| `tafel`, `ocr` | `tafel-pdf` and its `--ocr` / `--translate` |
| `llm` | catalog tagging proposals |
| `schema` | lectures.json validation |
| `legacy` | `get-lecture-images.py` |
| `edition` | the local per-book edition scripts |
| `ocrbook` | `uv run ocrbook …` (also needs Tesseract; Pandoc to build) |

Rendering with Microsoft Word (`word-pdf`, `contents-pages`) needs Word on macOS.
