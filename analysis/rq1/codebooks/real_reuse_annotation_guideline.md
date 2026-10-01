# RQ1 annotation guideline

This guideline is used to develop a benchmark and blueprint for MIST. It covers real reuse, binding paths and patterns, and binding locality. Each case starts from one exact PTM ID occurrence at a fixed repository commit. Real reuse means the code is configured to load or invoke that PTM through an eligible call. Static evidence does not prove execution at runtime.

## 1. Real reuse

Use [real_reuse_annotations.xlsx](real_reuse_annotations.xlsx), [fp_codebook.xlsx](fp_codebook.xlsx), and [reuse_codebook.xlsx](reuse_codebook.xlsx).

1. Open the occurrence link and locate the exact PTM ID. Check that the string is a PTM ID, rather than an unrelated string or a family filter. Apply the non-reuse rules before tracing.
2. Identify the variable, argument, default, field, configuration, or collection carrying the value. Use GitHub symbol navigation, with repository search when needed.
3. Follow that value through assignments, arguments, returns, wrappers, and imports, including across files and procedures. Inspect both how each connected code unit uses the value and where it is called, instantiated, or registered.
4. Stop at an eligible PTM-use call, a non-use endpoint, or a boundary that cannot be resolved. Verify the call signature, import origin, and context.
5. Record reuse, non-reuse, or uncertain and a short reason. For confirmed reuse, record the source, sink, import origin, and path. Include inline code or evidence links when useful.

Confirm reuse only when the exact value reaches an eligible sink. Defaults and selectable alternatives qualify when their paths are supported, regardless of the downstream task. Do not reject a case merely because it is in a test. Reject an apparent loader replaced by a mock or monkeypatch on that path. Apply the non-reuse codebook to comments, documentation, examples, unrelated strings, passive metadata, unused configuration, and other excluded contexts. Tokenization or cost accounting alone does not establish PTM loading. A closed-source PTM name mapped to a local model also does not qualify. If no connected endpoint can be identified with reasonable effort, record non-reuse and the reason. Use uncertain when the connected endpoint or its eligibility remains unclear.

Two annotators independently coded each round, then discussed disagreements and uncertain cases. They revised the guideline after Round 1 and applied it to new cases in Round 2. The workbook retains both annotations and final decisions. In the round sheets, `TRUE` means non-reuse, `FALSE` means reuse, and `NS` means uncertain.

## 2. Binding paths and patterns

For confirmed reuse, record ordered evidence links from source to sink in [binding_paths.xlsx](binding_paths.xlsx). Include intermediate transfers, not nearby unrelated code. One source reaching several sinks forms a separate binding per sink. Import origins verify sinks but are not automatically part of the value path.

Assign all supported operations using the 23 definitions in [binding_patterns.xlsx](binding_patterns.xlsx), counting each code once per case. The first author developed codes from a pilot and applied the revised codebook to all reuse cases. A second annotator reviewed proposed code sets as agree, partially agree, or disagree. Resolve revisions through discussion.

Keep these distinctions:

- A07 describes parameter defaults. A11 requires a class call transferring the value to a repository-defined `__init__` parameter. A17 describes forwarding through `super().__init__`.
- A03 describes dictionary storage, A09 retrieval, and A24 `**kwargs` expansion.
- A13 describes instance attributes, including configuration fields. A04 describes a configuration instance carrying the value across a component or call boundary.
- A06 describes environment defaults, while A08 covers other fallbacks. A18 describes provider branches, while A25 describes dynamic class selection.

Group codes under source, binding path, and sink using the workbook's role mapping. Roles can overlap, and a case need not have codes in all three. An SDK, provider, framework, or test context alone is not a pattern. Keep A15, A20, and A27 separate in annotations, even when reported together as `other`.

## 3. Binding locality

Record locality in [manual_binding_locality.xlsx](manual_binding_locality.xlsx). Count distinct path files, including source and sink files, once each. More than one file means the binding crosses files.

A procedure is a project function or method, including a constructor. Check value transfers through arguments, returns, constructor calls, inheritance, or use across methods. Module and class bodies are not procedures. A parameter default alone does not establish a crossing. A constructor storing the value for use in another method does. An imported SDK call alone does not establish a transfer into a project procedure.

A binding is local when it stays in one file without crossing project procedures. It is non-local when it crosses either boundary, and can cross both. Record the supporting transfer. Hops count value transfers between code elements, not files or procedures, so several hops may occur within one file or procedure.
