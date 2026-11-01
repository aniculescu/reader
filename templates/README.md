# Templates

| File | What it is |
| --- | --- |
| `print-template.docx` | The print layout every `.docx` build starts from (styles, page setup, footers, ornament). **This file is the versioned source**: edit it, not a copy elsewhere. |
| `print-template.styles.md` | Generated, readable summary of `print-template.docx`. It is the diffable view of every template change; never edit it by hand. |
| `ornament.png` | The lemniscate separator inserted under chapter headings. |
| `title-separator.jpg` | Title-page separator image. |
| `epub/` | EPUB stylesheets (`style/*.css`) and the EPUB ornament. |

## Changing the print template

1. **Edit.** Open `templates/print-template.docx` in Word, straight from the repo. Change the styles or page setup, and save it in Word Document (`.docx`) format. Don't attach a template to it (Tools → Templates and Add-ins), because Word records your local path when you do.
2. **Regenerate the summary:**
   ```sh
   python3 -m rsarchive.templatesummary templates/print-template.docx > templates/print-template.styles.md
   ```
3. **Check:**
   ```sh
   python3 -m rsarchive.cli docx-check templates/print-template.docx
   python3 -m unittest rsarchive.tests.test_template
   ```
   The test fails if the summary is out of date, if Word wrote anything it would reject, or if a local path got into the file.
4. **Commit both files together.** The summary's diff shows what changed, e.g. "FootnoteText: spacing after 0pt → 6pt".

Your Word user template `__RSLectures.dotx` is no longer the source. To use the repo's layout in Word for new documents, save a copy of `print-template.docx` as a Word template (`.dotx`) into Word's templates folder.
