# Binding pattern annotation guideline

This task describes how code introduces, selects, and carries a PTM ID to an eligible PTM-use call. Each case follows one exact ID occurrence along its confirmed binding path. Use the 23 code definitions and examples in [binding_patterns.xlsx](binding_patterns.xlsx) and the ordered evidence links in [binding_paths.xlsx](binding_paths.xlsx).

## Coding steps

1. Open the source and follow the linked locations to the sink at the fixed commit. Read the surrounding code, including relevant branches and called procedures.
2. Identify the operations that select or carry this ID. Assign every applicable code whose definition fits the evidence. A case may have several codes, but record each code only once.
3. Check for missing operations and remove codes unsupported by the path. Leave a short note with inline code or a link when useful.
4. For a second-author review, record agree, partially agree, or disagree with the proposed code set. Explain any additions, removals, or revisions. Discuss differences before finalizing the codes.

## Inclusion and exclusion rules

Code operations on the confirmed path, including defaults, selections, assignments, fields, arguments, returns, imports, and request construction. Include selectable alternatives when the evidence connects them to the sink. A provider, SDK, framework, HTTP call, or test context alone is not a binding pattern. Do not code unrelated operations elsewhere in the file or infer a transfer from nearby code.

Apply these distinctions from the final codebook:

- **Constructor transfer (A11):** A class call passes the value to a matching `__init__` parameter defined in the repository. A parameter default alone, `super().__init__` alone, or an imported sink with no visible constructor body does not qualify. Use A07 for parameter defaults and A17 for transfer through `super().__init__`.
- **Dictionary storage and expansion:** Use A03 for storing the value in a dictionary, A09 for retrieving it, and A24 for expanding a container with `**kwargs`. Assign several codes when the path contains the corresponding operations.
- **Configuration objects:** Use A13 for storing or reading an instance attribute, including configuration fields. A04 describes a configuration instance carrying the value across a component or call boundary.
- **Fallbacks:** Use A06 for an environment lookup supplying the ID as its default. A08 covers other conditional, Boolean, or lookup fallbacks.
- **Client selection:** A18 covers explicit provider branches. A25 covers retrieving or resolving a class dynamically and instantiating it with the model value. Neither label requires a formal design pattern.

## Binding roles and reporting

Group the assigned codes using `atomic_code_grouping` in the workbook. Source describes how the ID is introduced or selected, binding path how its value is routed, and sink how it reaches the eligible call. Codes can belong to several roles. Do not add a code merely to fill a role.

Keep the individual codes in the annotations. The reporting group `other` combines A15, A20, and A27 only when summarizing frequencies. These codes remain distinct.
