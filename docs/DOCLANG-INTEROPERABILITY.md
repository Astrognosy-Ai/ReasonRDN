# DocLang and Reason interoperability

**Status:** Implemented optional adapter; no external partnership or endorsement

DocLang and Reason solve adjacent parts of the same agent-memory problem.

- **Docling** converts real-world files into structured content.
- **DocLang** gives that content a compact, open, machine-oriented interchange
  representation.
- **Reason** assigns a stable `reason://` task address, accepts reusable
  contributions, retains provenance and immutable bytes, converges the
  available candidates, and resolves the current artifact before an agent
  repeats the work.
- **WARF Arbitration** produces a replayable selection event when candidates
  compete.
- **Xtend** evaluates the protected, versioned namespace-advancement policy
  without owning Registry pointers.

The portable composition is:

```text
PDF / Office / HTML / image / existing DocLang
                    |
                    v
          Docling -> DocLang bytes
                    |
                    v
       Reason contribution envelope
       - reason:// task address
       - exact content digest
       - original source digest
       - DocLang and converter versions
       - provenance and evidence context
                    |
                    v
        deterministic URI convergence
                    |
                    v
       cheap, verified future resolution
```

## Why the boundary matters

Reason does not parse, normalize, or reserialize DocLang. Existing `.dclg`
content is contributed byte-for-byte. The adapter records the DocLang format
version, implementation version, original source digest, output digest, and
validation method in the contribution identity. The Registry can therefore
cache, compare, replay, and resolve the representation without silently
changing it.

The envelope remains format-neutral. A Reason node does not require DocLang,
and a DocLang producer does not require Reason. This keeps the useful
interoperability surface open while allowing either ecosystem to evolve.

## Python use

An existing DocLang file needs no optional converter:

```python
from rdn import RDNClient, prepare_doclang_contribution

prepared = prepare_doclang_contribution("runbook.dclg")
result = prepared.contribute(
    RDNClient(),
    "reason://ops/deployment/rollback-plan",
    scope="organization",
)
```

For PDF, Office, HTML, images, and other Docling-supported inputs, install the
optional document dependencies and pass the source through the same function.
The resulting contribution is still ordinary Reason content with a declared
adapter binding.

## Linux collaboration surface

The smallest useful cross-project demonstration is an agent that:

1. resolves a `reason://` task before performing expensive document work;
2. uses Docling and DocLang only on a miss;
3. contributes the exact reusable DocLang artifact in the background; and
4. shows that a second runtime resolves and verifies the artifact without
   repeating conversion or reasoning.

That demonstration fits a vendor-neutral agent-interoperability conversation:
DocLang standardizes structured content exchange, while Reason standardizes
resolvable operating memory and replayable improvement around stable tasks.

Current upstream references:

- [DocLang specification and reference toolkit](https://github.com/doclang-project/doclang)
- [Docling project](https://github.com/docling-project/docling)
- [LF AI & Data DocLang Working Group announcement](https://lfaidata.foundation/press-release/2026/06/09/lf-ai-data-foundation-launches-doclang-specification-working-group-to-advance-an-open-standard-for-ai-native-documents/)
