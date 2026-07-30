# Research Gap

Five gaps are extracted from the survey and mapped to design responses. RG3 is the primary gap and
the others are supporting.

## RG1. Behavioural biometrics are modelled offline, not served

Papers 1, 2, 3 and 11 train and evaluate on static datasets with no serving architecture, latency
budget or throughput measurement. Authentication that cannot complete before a transaction commits is
not authentication.

**Response:** the scoring path is deployed on AWS and end to end latency is reported as a first class
result, separated into warm and cold invocation cases.

## RG2. Cloud fraud architectures omit behavioural signals

Papers 7, 8 and 15 build scalable pipelines whose payload is the transaction record. Behavioural
telemetry is high frequency, session scoped, unlabelled and per user, which demands a windowed
feature path and a low latency per user profile store that transaction oriented pipelines do not
provide.

**Response:** a per user profile store and a session scoped feature path are explicit architectural
components, and the deployment reports measured cost per thousand sessions, a figure absent from the
entire survey.

## RG3. The profile update mechanism is an unexamined attack surface (PRIMARY)

Papers 1, 3, 11, 12 and 13 recommend adaptive profiling to control false positives. None analyses the
security of the update rule. If a system updates a user's reference profile from sessions it scores
as genuine, then an adversary with valid credentials can submit sessions engineered to fall just
inside that threshold, shifting the profile incrementally toward their own behaviour. Over enough
sessions the adversary becomes the enrolled user, and the system has been taught to accept them. The
literature presents adaptivity as a pure improvement over static profiles; this project argues it is
a tradeoff and measures both sides of it.

**Response:** the attack is implemented and characterised, a guarded update policy is proposed as the
defence, and the poisoning resistance against false rejection tradeoff is reported as a curve over
the guard threshold.

## RG4. Decisions are binary and opaque, therefore operationally unusable

A hard fraud or not fraud output forces a bad choice: tighten the threshold and genuine customers are
blocked, loosen it and attackers pass. The survey reports accuracy and AUC but rarely a graded
response design, and rarely an explanation a human can act on.

**Response:** a calibrated 0 to 100 score mapped to four graded, reversible tiers, with every decision
returning the specific timing features that deviated, their direction and their magnitude.

## RG5. No adversarial evaluation against automated or replayed behaviour

The survey evaluates against historical fraud, which measures detection of past patterns. It does not
measure resistance to an adversary who knows a behavioural system is present.

**Response:** an attack simulator generating bot typing, session replay and impostor sessions, with
detection rate reported per attack class alongside conventional error rates.

## Consolidated gap statement

The surveyed literature recommends adaptive behavioural profiling without analysing the security of
the adaptation mechanism, evaluates behavioural models without deploying them, and reports neither
serving latency nor operating cost. This project addresses those gaps at a deliberately narrow scope:
one modality, one benchmark dataset, one deployed serverless pipeline, and one adversarial question
examined properly.

## Scope-narrowing justification

The scope is narrowed deliberately rather than by omission, and the narrowing is what makes the
primary question answerable within the project period.

- **One modality.** Keystroke dynamics only. Mouse and touch corpora share no subjects with any
  keystroke corpus, so multimodal fusion would have to be fabricated.
- **One benchmark dataset.** The CMU keystroke benchmark, because it supplies genuine impostor data
  from real humans and a published baseline that makes the reported error rates comparable rather
  than free floating. Because the benchmark is a fixed credential string, the evaluated claim covers
  credential entry and step-up re-authentication, not free-text continuous session authentication.
  Free text corpora (Buffalo, Clarkson) are deferred to future work.
- **One deployed pipeline.** A single serverless path on AWS within the always free tier, rather than
  an architectural comparison, so that latency and cost are measured on something real.
- **One adversarial question.** The security of the profile update rule, examined with an
  implemented attack, an implemented defence and a reported tradeoff curve, rather than several
  questions answered shallowly.
- **Supervised transaction fraud branch dropped.** IEEE-CIS and PaySim carry no behavioural signal,
  so a supervised transaction classifier would sit beside the behavioural work without informing it.

> The scope-narrowing text as written for the report (section 4.3 of the source document) is
> referenced but not reproduced in that document. TODO: replace the list above with the report text
> once available.

Related: [literature-survey.md](literature-survey.md), [project-documentation.md](project-documentation.md).
