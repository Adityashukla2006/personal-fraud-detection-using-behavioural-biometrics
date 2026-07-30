# Project Documentation

**Personal Financial Fraud Detection Framework using Behavioural Biometrics**

*Keystroke-Dynamics Continuous Authentication with a Poisoning-Resistant Adaptive Profile, Deployed Serverlessly on AWS*

- Course: Cloud Computing (Project Component)
- Deployment Platform: Amazon Web Services (AWS)
- Submitted by: Aditya Shukla | 23BIT0250, Arushi Tiwari | 23BIT0181
- SCORE, Vellore Institute of Technology, Vellore
- Guide: Dr. Priya V
- July 2026

This is the full source document, reproduced. Where a split document under `docs/` disagrees with
this file, this file is authoritative.

## 1. Abstract

Conventional financial fraud detection depends on static credentials such as passwords, one time
passwords and device fingerprints. These controls verify identity only at the point of login and
therefore fail once an attacker legitimately possesses the credentials, which is the outcome of
phishing, banking malware, SIM swap attacks and social engineering. Behavioural biometrics offer a
defence, because the manner in which a person types is difficult for an attacker to reproduce even
when the credential itself has been stolen.

This project implements a serverless behavioural authentication layer for a simulated internet
banking client on AWS. Keystroke timing is captured in the browser, standardised against a per user
reference profile, and converted into a continuous risk score from 0 to 100 that maps to four graded,
reversible responses: silent monitoring, step up re-authentication, transaction hold, and block with
alert. Each decision is accompanied by a feature level explanation naming the specific timing
features that deviated and by how much.

The research contribution concerns what happens when such a profile is allowed to adapt. Real users
change how they type, so a fixed reference profile produces rising false rejections and any practical
deployment must update the profile over time. This project shows that naive adaptation creates a new
attack surface: an adversary holding valid credentials can submit sessions calibrated to sit just
inside the update threshold and, over repeated sessions, gradually drag the reference profile toward
their own typing behaviour until they authenticate freely. A guarded update policy is proposed as a
defence, conditioning every profile update on sustained low risk and on the absence of credential,
device or authentication anomalies. The attack and the defence are evaluated on the CMU keystroke
dynamics benchmark, which supplies 51 subjects and therefore genuine impostor data, and the resulting
tradeoff between poisoning resistance and false rejection under genuine drift is reported as a curve
rather than a single operating point.

## 2. Problem Statement

Traditional financial fraud detection relies on transaction history, OTPs, passwords and device
authentication. These methods often fail when attackers obtain legitimate credentials through
phishing, malware, SIM swapping or social engineering, because once the credential check is passed
the session is trusted for its entire lifetime.

Four specific weaknesses motivate this work:

1. Authentication is an event, not a state. Verification happens once at login, so account takeover
   after successful login is invisible to the system.
2. Detection is retrospective. Rule based and transaction only models typically flag fraud after the
   transaction is submitted, when financial loss has already occurred.
3. Decisions are opaque and binary. A "fraud detected" flag with no reasoning creates high false
   positive friction for genuine customers and gives analysts nothing to act on.
4. Adaptivity introduces its own attack surface, and this is unexamined. Any behavioural system
   deployed for more than a few weeks must let user profiles evolve, otherwise genuine behavioural
   change is misread as attack. But an update mechanism that accepts sessions it judges genuine can be
   fed sessions that are deliberately just genuine enough. The literature proposes adaptive profiling
   as a solution to false positives without asking what it costs in security. That question is the core
   of this project.

## 3. Objectives

**O1.** Implement a browser based keystroke capture layer for a simulated banking client that records
timing and dynamics only, discarding key identity and never transmitting typed content.

**O2.** Build a serverless ingestion and scoring pipeline on AWS using only always free services,
with no idle cost and no provisioned infrastructure.

**O3.** Engineer a 12 feature keystroke representation (key hold times, inter key flight times, total
entry duration, correction rate), build per user reference profiles, and score sessions using a
scaled Manhattan distance over standardised features. Report Equal Error Rate, FAR and FRR on the CMU
keystroke benchmark against the published baseline for that dataset.

**O4.** Implement a risk engine that maps the anomaly score to a calibrated 0 to 100 value and four
graded response tiers, and returns a feature level explanation identifying the highest contributing
features by standardised deviation, with direction and magnitude.

**O5.** Characterise the profile poisoning attack against adaptive behavioural authentication,
implement a guarded update policy as a defence, and quantify the tradeoff between poisoning
resistance and false rejection rate under genuine behavioural drift. This is the primary research
objective.

**O6.** Build an attack simulator generating three adversarial session classes (constant interval bot
typing, session replay, and impostor sessions drawn from other benchmark subjects), and report
detection rate per class alongside measured end to end scoring latency and cost per thousand sessions
on the deployed system.

## 4. Literature Survey

### 4.1 Consolidated survey table

The 15 paper survey table is reproduced in full in [literature-survey.md](literature-survey.md),
together with the thematic synthesis.

### 4.3 Scope narrowing

Referenced in the work plan (phase P5) but not reproduced in the source document.
TODO: insert section 4.3 text. The derived summary is in
[research-gap.md](research-gap.md#scope-narrowing-justification).

## 5. Research Gap

Reproduced in full in [research-gap.md](research-gap.md): gaps RG1 to RG5 with their design
responses, RG3 marked primary, and the consolidated gap statement.

## 6. Proposed Framework and Architecture

### 6.1 Two track structure

The project is built as two deliverables that do not depend on each other.

| Track | What it is | Produces |
| --- | --- | --- |
| Track 1: Offline experiment | Python notebook over the CMU keystroke benchmark. Feature extraction, per user profiles, scoring, error rates, and the full poisoning and guarded update experiment | The research result: EER against the published baseline, and the poisoning resistance curve |
| Track 2: Live deployment | Serverless AWS pipeline serving the same scoring logic to a browser client, exercised with the team's own sessions and the attack simulator | The cloud result: a working demonstration plus measured latency and cost |

### 6.2 Deployed architecture

The diagram and the component-by-component description are in
[architecture-diagram.md](architecture-diagram.md).

## 7. Datasets and Evaluation

**Dataset.** The CMU keystroke dynamics benchmark, DSL-StrongPasswordData (Killourhy and Maxion,
2009). 51 subjects, one fixed password, 400 repetitions each across 8 sessions. This dataset is used
because it supplies genuine impostor data from real humans, requires no participant recruitment or
ethics approval, and has a published error rate baseline that makes this project's numbers directly
comparable to prior work rather than free floating. The published baseline for the best performing
detector on this dataset should be quoted from the source paper and used as the comparison point.

**Datasets considered and not used.** Buffalo and Clarkson free text keystroke corpora (needed for
genuine continuous session authentication, deferred to future work); Balabit mouse dynamics and
Touchalytics (no shared subjects with any keystroke corpus, so multimodal fusion would be
fabricated); IEEE-CIS and PaySim (no behavioural signal, and the supervised branch has been dropped).

**Metrics.**

| Category | Metrics |
| --- | --- |
| Detection | Equal Error Rate, FAR, FRR, ROC curve, compared against the published benchmark baseline |
| Poisoning | Sessions to successful impersonation, per update policy; profile displacement over time; resistance against false rejection tradeoff curve |
| Adaptation | False rejection rate under injected genuine drift, with and without adaptation |
| Robustness | Detection rate per attack class (bot typing, replay, impostor) |
| Cloud | End to end scoring latency at p50 and p95, reported separately for warm and cold Lambda invocations; measured cost per thousand sessions |

## 8. Work Plan and Status

| Week | Phase | Deliverable | Status |
| --- | --- | --- | --- |
| Prior | P1 | Problem identification, domain and scope definition | Completed |
| Prior | P2 | Literature survey: 15 papers tabulated across objective, methodology, models, limitations and improvement mapping | Completed |
| Prior | P3 | Thematic synthesis into four clusters, and identification of the shared blind spot on update security | Completed |
| Prior | P4 | Research gap formulation, five gaps with RG3 identified as primary | Completed |
| Prior | P5 | Scope narrowing decision with methodological justification (section 4.3) | Completed |
| Prior | P6 | Objective definition O1 to O6 | Completed |
| Prior | P7 | Threat model, attack definition and guarded update policy design | Completed |
| Prior | P8 | Serverless architecture, service exclusion analysis and cost model | Completed |
| 1 | P9 | CMU dataset ingestion, 12 feature extraction module, per user profiles, scaled Manhattan scoring, EER / FAR / FRR against the published baseline | In progress |
| 2 | P10 | Adaptive update loop, poisoning attack implementation, guarded policy, tradeoff curve, genuine drift injection | Planned |
| 3 | P11 | AWS deployment: static client and capture JS, API Gateway, score Lambda, DynamoDB schema, SNS alerting, scheduled updater Lambda | Planned |
| 4 | P12 | Attack simulator against the live endpoint, latency and cost measurement, results writeup, demonstration video, final report | Planned |

## 9. Individual Contribution

| Team member | Registration no. | Contribution area | Specific work |
| --- | --- | --- | --- |
| Aditya Shukla | 23BIT0250 | Cloud architecture and deployment | Serverless architecture design and AWS service mapping; service exclusion and cost analysis; API Gateway, score Lambda, DynamoDB schema, SNS alerting and the scheduled updater Lambda; browser capture layer with privacy encoding; attack simulator; latency and cost measurement; research gap analysis and scope narrowing justification |
| Arushi Tiwari | 23BIT0181 | Behavioural modelling and the poisoning experiment | Literature survey and thematic synthesis; CMU dataset processing; 12 feature extraction and per user profiling; scaled Manhattan scoring and calibration; EER, FAR and FRR evaluation against the published baseline; threat model formalisation; poisoning attack and guarded update policy implementation; tradeoff curve and drift injection experiments |

The expanded distribution, including shared responsibilities and the per folder ownership map, is in
[work-distribution.md](work-distribution.md).
