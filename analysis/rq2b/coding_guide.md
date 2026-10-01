# Integration-change annotation guide

Each case follows one matched PTM binding across two releases with unchanged PTM IDs and counts. We ask:

1. Did the integration change?
2. If yes, what changed?

Use the [review evidence HTML](inputs/evidence.html) to compare both releases. Read the relevant branch and surrounding code, not just the highlighted edits. Record the decision and leave a short note, with code or a GitHub link when useful.

## Task 1: Did the integration change?

- **change:** PTM selection, value transfer, configuration, access, invocation, or logical location changes.
- **unchanged:** none of these changes for the reviewed binding.

Follow the source to the first eligible PTM-use call, which may be a loader constructor. Include changes covered by C01–C08 below. Include new access options used by the binding's client or request, even if this caller leaves them at their default.

Exclude these when they are the only changes:

- Prompts, input preparation, generation settings such as temperature or token limits, response parsing, or output formatting.
- Logging, comments, type hints, formatting, or line-number shifts.
- A simple file or symbol rename that preserves the same integration.
- Edits to another provider branch, another binding, or code after the selected call that do not affect its invocation.
- A new parameter that is never read.

## Task 2: What changed?

For **change**, assign all supported codes. Multiple codes are allowed, with no priority order.

| Code | Type | What to look for |
| --- | --- | --- |
| C01 | PTM ID source | How the same ID is introduced or selected changes. |
| C02 | Configuration | PTM configuration or access options change, such as API keys, endpoints, or environment lookups. |
| C03 | Value flow | The PTM value passes through different variables, fields, mappings, arguments, or returns. |
| C04 | Wrapper | A project helper or class on the binding path is added, removed, or reorganized. |
| C05 | Invocation control | When or how the PTM-use call runs changes. |
| C06 | PTM-use call | The API call, PTM argument, or request construction carrying the PTM changes. |
| C07 | Code interface | The SDK, framework, cloud interface, or HTTP library used to access the PTM changes. |
| C08 | Logical relocation | The continuing integration moves to another file, procedure, class, or component beyond a simple rename. |

The annotation workbook contains the full definitions. For C05, identify a change to invocation in the reviewed code. Adding an option alone does not establish that change. Discuss changes that do not fit an existing code.
