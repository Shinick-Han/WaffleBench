# SEM data preparation (Carinthia-S)

Post-hackathon development, excluded from judging. This module prepares real
SEM defect images for later segmentation and review experiments. It doesn't
train or evaluate any model, and it doesn't change existing modules,
`RESEARCH_PROTOCOL.md` or `research-manifest.json`.

## Source and license

Carinthia-S dataset, Corinna Kofler (KAI GmbH) and Vahidin Hasić (University of
Sarajevo), Zenodo 2025, [doi:10.5281/zenodo.16895427](https://doi.org/10.5281/zenodo.16895427).
It is derived from the Carinthia dataset ([doi:10.5281/zenodo.10696644](https://doi.org/10.5281/zenodo.10696644)).
Both records are licensed [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/).
Any redistribution or derived result must credit the authors and the record, and
state the license. Every manifest carries this attribution in `license`.

The record's description (`carinthia-s_dataset.html`) documents 4,591 SEM
images of one unstructured production layer with expert-validated binary masks,
spread unevenly across six classes. The label table is `carinthia-s.csv` with
the columns `image_path`, `mask_path`, `filename` and `label`, and the files
are stored as `images/*.jpg` and `masks/*.png`. The description says class `6`
frames show no visible defect: they can occur when the SEM tool is misaligned.
They are real frames, not clean-wafer negatives. Class `5` has four images.

Raw data, extracted trees, manifests and summaries are run outputs. They are
never committed.

## Commands

Use an interpreter that has numpy and Pillow, for example the read-only
`tools/inspection-image-venv/Scripts/python.exe`:

```
python -m sem_data.cli extract --archive PATH/data.zip --root DATA
python -m sem_data.cli audit --data DATA --out OUTPUT
python -m sem_data.cli split --manifest OUTPUT/audit_manifest.json --out OUTPUT/split --seed 2026100501
python -m sem_data.cli validate --manifest OUTPUT/split/manifest.json --check-files
```

Each command prints one JSON status object. It returns a nonzero exit code on
failure. `audit` and `split` refuse to write inside the data root.

### extract

`extract` checks every entry before it writes anything. It rejects:

- absolute paths, drive or stream specifiers, `..` traversal, control characters and reserved device names
- symbolic links and other special files, as well as encrypted entries
- duplicate or case-insensitive colliding paths, and files that are also used as directories
- files over `--max-file-bytes` (default 64 MiB), an aggregate over `--max-total-bytes` (default 1 GiB) and more entries than `--max-entries`

Each entry is streamed with a byte limit that matches its declared size. The
output goes to a staging directory, which is renamed to `--root` only after
everything succeeds. A rejected archive leaves no output. The root must be
absent or empty. `.sem_data_extraction.json` records the archive SHA-256 and
the limits.

### audit

`audit` finds the single `carinthia-s.csv` below the root (or the only CSV that
has the required columns). Then it checks each row.

**Structural checks** reject the item and record the reason. The reasons are:
`path_not_contained`, `missing_image`, `missing_mask`, `missing_label`,
`duplicate_id`, `dimension_mismatch`, `nonbinary_mask`, `invalid_file`,
`undecodable` and `conflicting_duplicate_annotation`. A mask must contain
either two levels with a zero background (for example 0/255 or 0/1) or a
single level. RGB masks must have equal channels and a constant alpha.

**Semantic observations** never reject an item. For each class, the summary
counts empty masks and full-frame masks and gives the mean foreground fraction.
The source defines no per-class mask rule, so the audit doesn't invent one.

`image_sha256` and `mask_sha256` hash the file bytes. `duplicate_group` is
derived from the decoded pixel content, so identical images group together even
when they're stored as different files. Copies that share an image but have a
different label or a different binarized mask are rejected together.

The summary also reports class counts, dimension and mode counts, mask value
conventions and duplicate statistics. It includes the full list of rejections.
`created_at` defaults to the extraction time, which keeps repeated audits
byte-identical. You can override it with `--created-at`.

### split

`split` assigns `train`, `calibration` and `test` using fractions 0.7, 0.15 and
0.15 and seed `2026100501`. Allocation units are connected components of
duplicate groups and non-null source groups, so neither kind of group can cross
splits. Units are stratified by their majority class. Each stratum is shuffled
by a generator seeded from the seed and the class, and each unit then goes to
the split with the largest remaining item target. The result doesn't depend on
input order. The output is validated before it's written, and a split leak is
an error. `split.classes_absent` lists the classes that have no items in each
split. Per-class metrics for an absent class are unavailable (null) and are
never imputed.

### Mask policy

`audit` is strict by default (`mask_policy.mode: "strict-binary"`): masks with
intermediate grey levels are rejected as `nonbinary_mask` and reported, never
thresholded silently. `--soft-mask-threshold T` is an opt-in conversion for
development use only. It binarizes 0..255 soft-edge masks as `value >= T` and
records `mode: "soft-threshold"`, the threshold, `development_only: true` and
the provenance in the manifest. Readers must load masks through
`sem_data.audit.read_mask(manifest, item)` (or apply the same recorded rule) so
they use the same convention as the manifest they were given.

## Real archive audit (2026-10-05, strict policy)

`data.zip` (139,225,942 bytes, MD5 `3a41242574635cf1302fab15accb7730`,
SHA-256 `e7cc8507657611f89db33345d50fc7951e8784045684888dc997ac5298b7a3a2`)
extracted to 9,183 files. The label table `data/carinthia-s.csv` is
semicolon-delimited and has 4,591 rows. Every image is 480x480, 8-bit
greyscale.

| class | source rows | accepted | rejected (nonbinary mask) |
|---|---|---|---|
| 1 | 55 | 11 | 44 |
| 2 | 8 | 0 | 8 |
| 3 | 4008 | 3691 | 317 |
| 4 | 289 | 263 | 26 |
| 5 | 4 | 4 | 0 |
| 6 | 227 | 227 | 0 |

That's 4,196 accepted and 395 rejected. Every rejection is a soft-edge mask:
values range from 0 to 255, and a median of about 19% of the foreground pixels
have intermediate values. **Class 2 is excluded entirely** under the strict
policy, and class 1 keeps only 11 items. 226 of the 227 class 6 masks are empty.
No exact-image duplicates were found. The seed-2026100501 split is
train 2,938, calibration 631 and test 627. Class 5 is split 3/1/0, so it is
absent from the test split, and class 1 has a single test item.

## Manifest (schema_version 1)

The top-level fields are: `schema_version`, `dataset_id: "carinthia-s"`,
`data_mode: "real"`, `created_at`, `root` (absolute), `license`, `limitations`,
`split`, `items`, plus the auxiliary fields `units` and `source`. Each item
has these fields: `id`, `image` and `mask` (relative to `root`),
`defect_class`, `width`, `height`, `image_sha256`, `mask_sha256`,
`duplicate_group`, `source_group` (null when unavailable) and `split`. The
audit manifest has `split: null` on every item. The split manifest fills it in.

## Scientific limitations

- **Physical scale is unknown.** `units.pixel_size_nm` is null, and all sizes are in pixels.
- **Splits are image-level.** The source supplies no lot, wafer or acquisition grouping, so correlated acquisitions may fall into different splits. A `source_group`-style column (`source_group`, `lot`, `wafer`, `acquisition`, …) is used automatically when one is present.
- **Near-duplicates aren't detected.** Only exact decoded-pixel duplicates are grouped.
- **There are no clean whole-frame negatives.** None are synthesized. Background pixels in a defect frame don't substitute for clean frames.
- **Class coverage is limited.** Class 2 has no accepted items under the strict policy. Classes 1 and 5 are scarce, and class 5 has no test items. Per-class estimates for them are unavailable or unreliable.
- **The test split is pristine.** Don't use it for training, calibration, thresholds, model selection or inspection until one final pre-registered evaluation. `sem_data.split.inference_view` exposes only image identity (no mask, no label), and it refuses the test split unless that is explicitly requested.

## Checks

```
tools/inspection-image-venv/Scripts/python.exe -m unittest tests.test_sem_data_zip tests.test_sem_data_audit tests.test_sem_data_split
```
