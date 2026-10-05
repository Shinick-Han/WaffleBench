# Data-efficient WaffleBench development

Post-hackathon development — not part of the submitted version.

New packages preserve existing scientific modules and the submitted version:

- `sem_efficiency`: real SEM frozen-feature cache, small output-head learning and an own masked-feature reconstruction adapter using selected NFFA material images.
- `adaptive_route`: seven-parameter residual actor trained with Monte Carlo policy gradients and a learned value baseline on new synthetic episodes. This is not CQL, PPO or real-log offline RL.
- `workflow_learning`: grouped chronological prediction of IPI recorded task working hours using request descriptors. This does not predict SEM site cost or validate physical schedules.
- `verify_data_efficiency.py`: independent reward, cost, pixel-count and MAE arithmetic checks on preserved artifacts.

## Execution

The isolated environment is `C:/Users/user/hacknation7th/output/post-hackathon/data-efficient-20261005/env`.
It reads dependencies from existing SEM/inspection site-packages through `.pth` files. Existing environments were not installed into or upgraded. This local dependency reuse is not a portable standalone install. `environment.json` in the output directory records observed versions. No weights are downloaded at inference.

From this checkout, using the isolated environment's Python:

```powershell
python -m sem_efficiency.experiment --manifest MANIFEST --base-model FROZEN_MODEL_DIRECTORY --acquisition ACQUISITION_DIRECTORY --out FRESH_SEM_OUTPUT
python -m adaptive_route.experiment --out FRESH_SYNTHETIC_OUTPUT
python -m workflow_learning.experiment --csv IPI_CSV --out FRESH_WORKFLOW_OUTPUT
```

Output directories must be new. Existing attempts are not overwritten or resumed. The SEM runner has no original-test execution option. Predictions are persisted before scoring; frozen protocols and source/model hashes are retained. Registered readers exclude original test data, but unrestricted processes can still read the shared filesystem. This is not OS-enforced blindness.

## Completed evidence

Artifacts: `C:/Users/user/hacknation7th/output/post-hackathon/data-efficient-20261005/`.

- `experiment-v1`: 128 train, 32 tune, 64 development evaluation images and 64 NFFA self-supervised frames. Original 627-image test untouched.
- `routing-development-v1`: two training seeds; 192 unique generated training lots reused across budgets/epochs (1,536 training episode executions); 640 comparisons on 64 fresh same-generator development lots.
- `workflow-development-v1`: 1,476 usable jobs; 962 train, 259 tune, 255 chronological evaluation; 170 rejected jobs documented.
- `independent-verification.json`: arithmetic, prediction/model hashes, budgets, unknown outcomes and partitions.

SEM adaptation did not beat the frozen baseline. All eight learned routing versus risk-per-second intervals include zero. The tuning-selected workflow predictor did not improve evaluation MAE over the training-median baseline. No new learned candidate is adopted as a default. Unfavorable results and both training seeds are retained.

SEM component recall was 43/43 at IoU >=0.25 in the selected development pool, with three false predicted components. Twenty-one empty-mask frames had no predicted components, but are not representative normal wafers. No defect below 256 pixels occurred. This cannot establish small-defect or full-wafer performance.

Prioritize richer training-only representation/perception ablations and matched optical/SEM review data. Work-hour predictors need better timestamp-valid request/process descriptors and missingness handling. Fresh independent evaluation is necessary for a later selected release. No public or submitted artifacts were updated.
