# Work Distribution

## 1. Responsibilities

| Team member | Registration no. | Contribution area | Specific work |
| --- | --- | --- | --- |
| Aditya Shukla | 23BIT0250 | Cloud architecture and deployment | Serverless architecture design and AWS service mapping; service exclusion and cost analysis; browser capture layer with privacy encoding; API Gateway and score Lambda; DynamoDB schema; SNS alerting; scheduled updater Lambda; attack simulator; latency and cost measurement; research gap analysis and scope-narrowing justification |
| Arushi Tiwari | 23BIT0181 | Behavioural modelling and the poisoning experiment | Literature survey and thematic synthesis; CMU dataset processing; 12-feature extraction and per-user profiling; scaled Manhattan scoring and calibration; EER, FAR and FRR evaluation against the published baseline; threat model formalisation; poisoning attack and guarded update policy implementation; tradeoff curve and drift injection experiments |

## 2. Shared responsibilities

Three items are shared and are not owned by either member alone.

**The shared feature and scoring module (`backend/shared/`).** The 12 feature definitions, the
standardisation step and the scaled Manhattan distance live in one module imported by both the
offline analysis and the score Lambda. It is jointly owned because it is the single point where the
two tracks meet: a unilateral change to a feature definition would silently break the claim that the
evaluated scorer and the deployed scorer are identical. Changes here are agreed by both members
before commit.

**The final report.** Aditya writes the architecture, deployment, cost and adversarial evaluation
sections; Arushi writes the survey, modelling, poisoning and results sections; abstract, problem
statement, objectives and conclusion are written jointly.

**The demonstration.** The recorded demonstration and the review presentations are prepared and
delivered jointly: Aditya drives the live deployment and the attack simulator, Arushi presents the
research result and the tradeoff curve.

## 3. Per-folder ownership map

Unambiguous, so it is clear who commits where.

| Folder | Owner | Notes |
| --- | --- | --- |
| `frontend/`, `frontend/src/` | Aditya | Client and capture layer |
| `backend/lambdas/score/` | Aditya | Scoring Lambda |
| `backend/lambdas/updater/` | Aditya | Guarded updater Lambda, guard policy specified jointly |
| `backend/shared/` | Shared | Both members agree changes before commit |
| `ai-models/feature-extraction/` | Arushi | Applies the shared module to the benchmark |
| `ai-models/profiling/` | Arushi | Reference profiles and calibration |
| `ai-models/experiments/` | Arushi | Evaluation, poisoning, guarded update, drift |
| `database/` | Aditya | Single-table design and schema |
| `data/raw/`, `data/processed/` | Arushi | Dataset workspace, contents not committed |
| `docs/literature-survey.md` | Arushi | Survey and thematic synthesis |
| `docs/research-gap.md` | Aditya | Gap analysis and scope-narrowing justification |
| `docs/architecture-diagram.md` | Aditya | Architecture and component descriptions |
| `docs/project-documentation.md` | Shared | Reproduction of the source document |
| `docs/work-distribution.md` | Shared | This file |
| `results/figures/`, `results/tables/` | Arushi | Detection and poisoning output; latency and cost output committed by Aditya |
| `presentation/` | Shared | Review slides and demonstration recording |
| `README.md`, `.gitignore` | Shared | Repository root |
