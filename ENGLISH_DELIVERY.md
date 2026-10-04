# English-language delivery

All six current demo pages use English headings, controls, explanations, chart labels, accessible names, loading/error/retry states and metadata. The PVT workbench loads the English presentation companion `web/app.en.js`; the hash-bound original `web/app.js` remains an archival baseline. Synthetic wafer descriptions are translated at display time, without changing the dataset.

The root research, implementation, improvement and result documents are in English. Two hash-bound Korean protocols remain untouched for provenance; their complete English reading copies are `RESEARCH_PROTOCOL.en.md` and `INSPECTION_PROTOCOL.en.md`. The illustrative mockup is available in English at `mockup/index.en.html`.

Stored scientific JSON, original datasets, ledger events, model artifacts, frozen source files and exact-byte downloads are unchanged. A raw download can retain archival Korean descriptions. Display translations do not create a new scientific run or change a verdict.

All three submission videos have English narration and captions. The product demo was re-rendered with two new English screenshots of the actual stored replay. It is still 59.6 seconds; the participant and technical videos remain 50.0 and 58.0 seconds. Encoding, duration, audio presence and hashes were rechecked.

The concise English submission text is in `SUBMISSION_EN.md`; full results remain in the individual study reports and `OPTIMIZATION_3H.md`.

Verification covered all six pages at 390, 768 and 1440 pixels, replay controls, all three synthetic wafers, expanded descriptions and exact-byte JSON export. Additional checks covered the records filters at 390 pixels and the synthetic wafer page at 360 pixels with 200% zoom. The focused frontend/server suite passed 36 tests. A separate English stylesheet handles longer translated text while the original frozen CSS stays unchanged. Static publishing includes the English assets and disables local execution through the existing static-mode metadata.
