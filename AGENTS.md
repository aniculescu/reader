# Reader project standing instructions

These instructions apply to every task in this project.

## Evaluate effort before work

- Before starting substantive work, assess the task's complexity, uncertainty, and risk, and choose an appropriate reasoning effort and verification depth.
- Reassess when the scope or difficulty changes. Use lighter effort for routine steps and deeper effort for difficult translation, source reconciliation, completeness checks, and document integrity problems.
- Distinguish an assessment or recommendation from an actual model or effort-setting change; do not claim a setting changed unless it was changed through an available control.

## Working conventions

- Read `docs/agent-notes.md` before building or changing editions, the toolkit, or Word documents. It covers the public-repo rules, .docx generation and verification, Word automation, and rsarchive.org quirks.
- Record conventions that anyone could reuse there, and private context (identities, local paths, project state) in the private overlay's memory.

## Maintain the translation dictionary

- Consult the project's anthroposophical German dictionary whenever relevant to translation, terminology review, or corrections.
- Current dictionary: `.private/dictionary/Anthroposophy German Dictionary - Overview.csv` in the private overlay (see `docs/agent-notes.md`). `~/Downloads/Anthroposophy German Dictionary - Overview.csv` is a symlink to it for older scripts. Edit the file, not the link.
- Incorporate relevant, source-supported additions and corrections into the existing dictionary as part of the same task, rather than leaving them only in a report or temporary copy.
- Ask the user instead of guessing when the meaning, preferred translation, or suitability of a dictionary change is uncertain. Do not record an unconfirmed interpretation as settled terminology. Continue unrelated work while awaiting clarification when possible.
- Preserve valid existing meanings and add contextual qualifications for sense-specific translations. Do not replace every occurrence or overwrite a general definition on the strength of one passage.
- Clearly state if the dictionary cannot be accessed or updated. At handoff, state whether it was updated and briefly identify any changes or pending questions. Never claim an update without verifying the saved file.
