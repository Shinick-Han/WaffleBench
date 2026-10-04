# Wafer Field Inspection Bottlenecks and an AI Precision Review Research Proposal

A research memo based on public primary sources checked on October 4, 2026. It distinguishes tool vendors' announcements, research paper results and our project's proposals. It is not a field demonstration with a specific fab's recipes, prices or throughput logs.

The recommended problem is **confirming important defects and the existing model's misses faster, using limited precision inspection time, among the many suspect candidates produced by fast inspection**. The core of the presentation is the process of choosing the inspection order and changing the next choice based on confirmed results. The tool's sensor performance, the existing classification AI and our additional selection policy are each evaluated separately.

This proposal is follow-up research for a separate wafer inspection extension. It does not modify the finalized PVT research protocol or its results, nor repurpose them as wafer inspection results. No model/harness implementation or experimental results exist yet.

## Real inspection flow

A flow in which in-process optical inspection finds suspect locations and some of them are examined in detail by electron-beam review is described by real tool vendors. Inspection candidates can include real defects, harmless pattern/process variations and image noise together. The candidate map and the confirmed defect map are different outputs. [Applied Materials SEMVision H20 announcement](https://ir.appliedmaterials.com/news-releases/news-release-details/applied-materials-accelerates-chip-defect-review-next-gen-ebeam/)

| Stage | Observation obtained | Tool examples | Role in our experiment |
| --- | --- | --- | --- |
| Optical screening inspection | Suspect locations, optical signal, defect candidates | KLA 3935, Voyager 1035, 8935, Applied Enlight | Fast but incomplete initial information |
| Precision e-beam review | SEM images of selected locations, morphology, material, voltage contrast, etc. | KLA eDRX1, Applied SEMVision H20, Hitachi CR7300 | An action that pays a cost to acquire new evidence |
| Metrology | Values such as CD and overlay | ASML YieldStar 1375F, etc. | A separate measurement path for pattern dimension deviations |
| Electrical test | Electrical pass/fail per test condition | Wafer probing and EDS | Electrical function results and spatial bin maps |
| Analysis and action | Defect distribution, tool history, root-cause candidates | KLA Klarity, SPOT | Supporting investigation priorities and engineer judgment |

The tool roles are summarized based on [KLA inspection and review products](https://www.kla.com/products/chip-manufacturing/defect-inspection-review), [ASML metrology products](https://www.asml.com/en/products/metrology-and-inspection-systems/yieldstar-1375f), [FormFactor wafer measurement products](https://www.formfactor.com/products/) and [KLA analysis products](https://www.kla.com/products/software-solutions/semiconductor). The link between electrical testing and WBM is also described in [the original 2022 paper by Korea University researchers](https://jkiie.org/_common/do.php?a=full&aidx=32830&b=12&bidx=2918). This does not mean every fab uses the same order, frequency and tools.

Electrically testing 1305 dies and inspecting the many fine locations inside those dies with precision imaging are different scopes. A budget of 35 can therefore be explained as the budget of a specific follow-up review action. It must not be described as inspecting only 35 dies on a wafer to guarantee overall quality.

## Tool performance confirmed from public sources

Below are conditional numbers from manufacturer announcements. Resolution, detectable defect size, design node, throughput and classification accuracy are different metrics. The overall miss rate or false-positive rate of a specific product cannot be computed from these numbers.

| Product or study | Published numbers/features | Scope of interpretation |
| --- | --- | --- |
| KLA 3935, 3920 EP | Targets such as ≤5nm logic, linking optical inspection with e-beam, ML-based nuisance separation | The target design node is not a minimum detection size or detection rate. [Product page](https://www.kla.com/products/chip-manufacturing/defect-inspection-review) |
| ASML HMI eScan 1100 | Describes 25 beams, up to 15× throughput versus single-beam inspection, pattern defect detection down to 7nm | A combination of parallel beams, stage and computation technology. Not an AI-only improvement rate or a detection guarantee under all conditions. [Product page](https://www.asml.com/en/products/metrology-and-inspection-systems/hmi-escan-1100) |
| Applied SEMVision H20 | Sub-nm image resolution, an announcement of cutting the time to acquire the same information to 1/3, deep-learning classification | Includes improvements to the source and imaging hardware. It cannot be said that classification AI alone gave a 3× improvement. [2025 announcement](https://ir.appliedmaterials.com/news-releases/news-release-details/applied-materials-accelerates-chip-defect-review-next-gen-ebeam/) |
| Hitachi CR7300 | 2× throughput versus the previous model, AI-based automatic defect classification | The public page has no absolute figures for classification accuracy or miss rate. [Product page](https://www.hitachi-hightech.com/in/en/products/semiconductor-manufacturing/cd-sem/dr-sem/cr7300.html) |
| ASML YieldStar 1375F | nm-level CD and overlay metrology, faster measurement than SEM, ML-based metrology robust to stack variation | This is dimensional measurement performance. It is not the same as defect classification accuracy. [Product page](https://www.asml.com/en/products/metrology-and-inspection-systems/yieldstar-1375f) |

This public-source survey did not obtain unified ROC curves measured on the same product, layer, defect type, size and inspection speed, or a reproducible absolute wafers/hour comparison table. We therefore do not arbitrarily set a tool's detection rate to 82% or make up prices or inspection seconds as if they were field values. A real comparison needs the recipe, layer, ROI area, defect size and contrast, dwell/dose, move and loading time, confirmation labels and repeat-measurement conditions.

## Bottlenecks and where AI can contribute

| Bottleneck | Role of AI or a selection policy | Metric to verify |
| --- | --- | --- |
| Optical candidates contain much nuisance | Confirm important candidates first using image, signal and pattern context | Number of confirmed important defects in the same review time, review precision |
| Time and throughput limits of precision review | Reduce duplicate candidates and consider spatial/pattern diversity together with cost | Valid discoveries/total time, coverage of selected locations |
| Bias toward known defects misses new ones | Additionally inspect uncertain or out-of-distribution candidates and some candidates confidently judged normal | Time to first discovery of a new type, number of confirmed miss counterexamples |
| Engineer work to create image labels | Reduce the confirmation and labeling needed through transfer learning and active learning | Confirmation cost to reach target performance, per-type recall |
| Records of multiple tools and processes are siloed | Link inspection, metrology and tool history and trace evidence | Time to confirm the real cause; root-cause recommendations are marked as hypotheses |

It is not that the field has no AI. KLA SPOT already optimizes review samples with ML and statistics, and Klarity performs defect and process analysis. We therefore do not claim simple AI priority recommendation itself as a new invention. The proposed differentiators are an independent audit that links per-tool evidence, falsification search for new patterns, and comparing selection, cost and confirmation results in a reproducible harness. Superiority over competing products requires separate verification. [KLA SPOT and Klarity](https://www.kla.com/products/software-solutions/semiconductor)

This division of roles is our research proposal derived from the surveyed sources. Defects with no signal in the image, or not included in the initial candidates, cannot be recovered just by reordering candidates. Finding out-of-candidate misses requires adding extra observations such as a separate care-area rescan as an action, including its cost.

## Evidence that can be cited about AI effects

| Evidence | Observed or announced result | Claim we allow in our presentation |
| --- | --- | --- |
| Applied ExtractAI 2021 | Manufacturer announcement of reviewing 0.001× of candidates, i.e. 0.1%, and then classifying all candidates | A commercial flow linking a few precision confirmations with large-scale candidate inference exists. It does not mean a 0% miss rate or a 1000× improvement for our system. [Original announcement](https://ir.appliedmaterials.com/node/24121/pdf) |
| KLA Investor Day 2022 optical/e-beam linking case | Announcement of a 2× improvement in important defect detection on a specific critical metal layer | An example of the effect of a linked inspection flow. Not extended to an AI-only effect or a figure applicable to all layers. [Original slide 47](https://ir.kla.com/sec-filings/all-sec-filings/content/0001193125-22-175500/d341367dex991.htm) |
| IBM researchers' 2025 SEM classification study | About 7400+ SEM images, two inspection layers, 11 types. With fine-tuning, over 90% classification accuracy with 5 and 15 labels per type respectively | The possibility of classifying real fab images with few labels. The same wafer was kept from spanning train/test. Not a whole-wafer detection recall or error-rate improvement figure. [Original paper](https://arxiv.org/pdf/2506.03345) |
| Hu et al. 2024 dicing image study | Balanced accuracy from 65.1% with original training to 88.2% with DCGAN augmentation, a difference of 23.1%p | A preliminary result of a study on limited dicing defect images. Synthetic-image augmentation results are not carried over to nanoscale process inspection or the current synthetic tabular data. [Original paper](https://arxiv.org/pdf/2407.20268) |
| Korea University researchers' 2022 WBM active learning study | Selective labeling including new patterns evaluated on WM-811K | Grounds for evaluating new-pattern discovery and labeling efficiency together. Spatial bin-map classification is a different problem from SEM defect detection. [Original paper](https://jkiie.org/_common/do.php?a=full&aidx=32830&b=12&bidx=2918) |

These results are not averaged or multiplied into a single AI improvement rate. Hardware, inspection linking, image classification, labeling efficiency and yield are each reported against their own baselines and units. The size of our system's improvement has not yet been measured.

## Recommended presentation story

A process engineer has a candidate map from fast inspection and limited precision review time. There are already classification scores, but candidates of the same pattern may be confirmed repeatedly, or a new process anomaly may be judged normal. WaffleBench chooses the most valuable next inspection given current evidence and an inspection that could refute existing judgments. It takes in confirmed results, updates priorities, and compares against a fixed plan with the same budget. The engineer can see which locations were inspected and why, what was confirmed, and where there is still no evidence.

The key scene of this story is not coloring uncertain points but **inspection action → new observation → disagreement with the existing judgment → change of the next inspection**. The scene of catching a confident misjudgment in images of a new type or under data shift connects to Falsify's falsification theme.

The LLM's role is to explain investigation hypotheses from limited observations and to call permitted tools. Risk scores and uncertainty are computed in evaluable models, and observation results and budget execution are recorded by the harness. It provides grounds for the engineer to decide lot disposition or process changes. Natural-language explanations are not stored as sensor observations or confirmed causes.

## Hypotheses the harness must test

The following is a proposed follow-up experiment design and does not change the prespecified criteria of the existing PVT study.

The primary hypothesis is that, for the same total precision review time, adaptive selection finds more confirmed important defects than a strong fixed baseline. The secondary hypothesis is that, under new patterns and distribution shift, it finds confirmed false negatives of the frozen initial model faster. Defect discovery and model-error discovery serve different purposes, so the two curves are reported separately.

| Compared policy | Reason for comparison |
| --- | --- |
| Random selection | Minimal control; not used as the sole comparator for a claim |
| Fixed tool/recipe-like score ranking | A realistically possible score/size/signal criterion. No claim of reproducing a vendor product |
| Importance ranking from a fixed learned model | Separates the effect of adding AI classification from the effect of sequential selection |
| Uncertainty/diversity-based active selection | The existing strong AI sampling control |
| Falsify adaptive selection | Reflects confirmation results, spatial diversity, cost and miss-audit actions |
| Component ablation | Removes the audit action, spatial context and updates one at a time to assess their contributions |

All policies receive the same initial information, initial model, allowed actions and inspection budget. SEM results or electrical measurements of not-yet-selected locations and the generated ground truth are hidden from the inputs. Only the information available when initial optical inspection is complete is accepted as the free initial state. Training, update and inference costs and move, loading and retry times are also recorded separately.

The primary metric is the number of confirmed DOIs within budget and the capture of that candidate population. This is not to be confused with field-wide defect recall. End-to-end evaluation that puts out-of-candidate defects in the denominator needs separate exhaustive ground truth and a rescan path. Labels also distinguish defects confirmed by morphology from defects whose yield impact is proven by electrical testing or failure analysis.

Secondary metrics are the cost to discover a new type, the number of frozen-model false negatives found, per-type recall, the nuisance review rate, the number remaining unconfirmed, and dropout/failure costs. Overall recall is not estimated high by evaluating only selected samples. The exhaustive evaluation ground truth is used for aggregation only after policy execution ends.

First, discovery/cost curves are compared across several budgets. The time reduction for the same discovery goal is defined as `1 - AI time / baseline time`, and the discovery increase for the same time as `AI discoveries / baseline discoveries - 1`. If the geometric cost of moving between candidates is included, 35 reviews and the actual time budget may not be the same.

The proposed criterion for business viability is a 20% relative increase in discoveries for the same time, or a 30% reduction in total cost for the same confirmation goal, versus a strong baseline. **Neither is a goal achieved yet**, and adoption and the unit of computation must be fixed before verification. If cost savings increase misses of important defects, it is not treated as success. If no required customer recall standard is available, an arbitrary 95% is not written down as an industry standard.

Evaluation data are separated by lot, wafer and time, with separate experiments for new types, signal contrast, nuisance density, defect frequency, observation dropouts and recipe changes. Confidence intervals are computed by paired comparisons at the independent-lot level, not by treating the 3915 dies as independent samples. One 3-wafer lot is kept as the UI demo, and independent lots must be added before the main comparison. Thresholds, detection probabilities and seeds are not chosen to fit the test results.

## Changes needed in the current synthetic data

The current data include tabular electrical and metrology values, generated defect labels and stochastically generated simulated detections. It is not a real inspection log output simultaneously by a single tool. The generated simulated detections were made with an 82% per-defect probability and a 0.8% extra-detection probability, and are not values calibrated from tool measurements.

On the current 3915 dies, the simulated detection recall for defect presence is 82.67% and the precision is 96.62%. These are neither results of a new model nor reference performance of a field tool. Agreement with bins computed from the 40 raw inspections is an implementation check of the judgment rules. These two figures are not automatically promoted to baselines for the research hypotheses.

| Data or field to add | Why it is needed |
| --- | --- |
| candidate/site/layer identifiers and in-die coordinates | The actual follow-up review unit may differ from the whole die |
| Optical signal, image features and initial candidate membership | Separates screening from precision confirmation and also evaluates screening misses |
| Tool, chamber, time and lot history | Examines new distributions and tool-association hypotheses |
| Observation results revealed only after selection | Prevents leakage from reading future inspection results in advance |
| Per-action observation quality, cost, dropouts and failures | Separates sensor performance from selection policy effects |
| Separate labels for morphology confirmation, electrical impact and confirmed root cause | Prevents confusing physical defects with electrical failures and root causes |

Experiments on tabular data without real SEM images are stated as inspection scheduling selection simulations. They are not described as an image classification model having seen real microscopic defects. The sensor model's misses and false positives are built as assumptions that vary with defect type, size, contrast and inspection method, and are compared across several conditions. It is not implemented so that the sensor's detection probability rises when only the AI policy is changed.

## Items to decide before implementation

1. Fix the primary user and action as a process engineer's selection of follow-up reviews.
2. Split the ground truth into morphology-confirmed DOIs and model miss counterexamples, and define the denominator of each.
3. Decide the initial information and paid observations, the out-of-candidate rescan path and the cost unit.
4. Fix the strong baselines, the scope of sequential updates and the miss-audit share before seeing results.
5. Check the permissions and lot information of public research data and the feasibility of obtaining field logs.
6. Present only real numbers that passed the comparison, and leave field metrics that could not be obtained as assumptions.

The outputs of this survey are the research direction above and its evidence. The existing UI, data and models were not automatically replaced and the public demo was not changed.
