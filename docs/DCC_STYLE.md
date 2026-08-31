# DCC writing benchmark (evidence from 13 accepted papers, 2022-2026)

Source: full-text analysis of 13 accepted DCC papers (7 classical/stringology
"Track A", 6 systems/tool+benchmark "Track B"). OUR PAPER IS TRACK B.
Raw analysis: see session record 2026-08-31.

## Hard format rule
**10 pages TOTAL** — including references, figures, tables, notes, AND
appendices. 12pt, single column, 9in x 6in text area. Verified against DCC
2025/2026 programs (proceedings page numbers step in exact 10s).
=> An appendix is NOT a way to buy space. Overflow goes to the repo.

## Target shape (Track B median, re-weighted for a diagnosis+tool+benchmark paper)

| Section | Pages | Words |
|---|---|---|
| Title/authors/abstract | 0.40 | 110-130 (abstract) |
| 1 Introduction (ends with contributions IN PROSE) | 1.00 | ~500 |
| 2 Related Work | 0.75 | ~375 |
| 3 Background / problem setup | 0.85 | ~425 |
| 4 Diagnosis | 1.40 | ~700 |
| 5 Method / tool | 1.60 | ~800 |
| 6 Evaluation methodology (separate, short) | 0.75 | ~375 |
| 7 Results (roadmap para + 3-4 numbered subsections) | 2.10 | ~1,050 |
| 8 Conclusion | 0.25 | ~125 |
| Acknowledgements | 0.10 | - |
| References (18-24 entries) | 1.40 | - |
| **TOTAL** | **10.0** | **~4,500-4,800** |

Checkpoints: intro ends p1.4 | related work ends p2.2 | method ends p5.6 |
first result number by p6.6 | references start by p8.6.

## Density
- 4-5 figures, 2 tables (6-7 floats max). Evaluation is figure-led.
- 0-1 numbered equations (0 in 11/13). Complexity/cost claims go inline.
- 18-24 references. Each costs ~0.06pp at 12pt single column.
- Paragraphs: 3-5 sentences, 48-66 words. Split anything past ~120 words.

## Abstract formula (110-130 words, 6 sentences, problem-first)
1. Context (the setting).
2. Problem: "However, <the failure>."
3. Gap: "Existing approaches <what they rely on>, which <what they lack>."
4. "This paper presents <NAME>, a <one clause>." + benchmark scope.
5. Headline number vs a NAMED baseline.
6. Secondary number or honest scope bound.
**Every Track B abstract ends on a number. None ends on a novelty claim.**

## Rules derived from the corpus
1. Problem-first abstract ending on a number. (6/6 Track B)
2. Separate Related Work section IS in-format for Track B (3/6) but cap at 0.75pp.
3. DO add a short standalone "Evaluation Methodology" section (~0.75pp), then
   keep Results clean. (MLcomp, LICO)
4. NO Limitations / Discussion / Threats to Validity / Reproducibility /
   Ethics sections. **0/13.** Limitations go inline: ~4 sentences total, 0 headings.
5. NO appendix. **0/13**, and it counts against the 10 pages.
6. Cap bibliography at 18-24.
7. Results: one roadmap paragraph, then 3-4 numbered subsections, one per claim.
8. Background opens with a definition, not with why definitions matter.
9. Contributions in PROSE in the last paragraph of section 1 (12/13). Not a
   bulleted block with bold lead-ins - that is an ML convention absent here.
10. Code link = ONE sentence inside the evaluation, with a commit hash. No
    availability section, no badge. (~5/13 include one)
11. Title form: "NAME: What It Does" (LICO, HOLZ, HyperCSA, Machete, LZHV).
12. Conclusion ~0.25pp. State what is next and what is still slow; do not
    restate contributions.
13. First-person plural throughout ("we/our" roughly every 40-130 words).
14. Negative/honest results stated plainly inline, e.g. "Within our
    experimental configuration, Nielsen's algorithm outperforms Algorithm 2."

## Positioning note
DCC's practical track is dominated by string algorithms, indexing, and
self-contained compressor engineering. The HPC/floating-point group
(SZ/cuSZ/SPERR/zfp) barely publishes here - that genre lives at SC/HPDC.
DCC open-access rate is ~10%; author homepages are far richer than any index.
