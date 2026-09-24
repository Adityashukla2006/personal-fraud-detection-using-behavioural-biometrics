# Demo walkthrough

About ten minutes, on the deployed dev stack. Open the client URL from `terraform output client_url`
on a laptop with a passkey-capable browser, and keep **Under the hood** open on the right: it shows
every request's route through AWS, its measured latency, the decision, and the exact payload sent.

## Before the demo

1. Sign in once with your test account and add a passkey (Security, Add a passkey).
2. Mark the laptop's browser as an enrolled device, so a passkey step-up there earns full trust:
   `python simulator/demo_scenarios.py enrolled-device --email <you>`.
3. Make one small transfer, so there is history and a confirmed session to replay later.

## The story

1. **Sign in.** The map lights Cognito, then API Gateway, λ scoring and DynamoDB for the login
   check. Point at the waterfall: the Lambda itself takes about 25 ms; the rest is API Gateway and
   the network.
2. **What leaves the browser.** Open the payload panel: timing arrays and three counts, no
   characters, no key codes, and the payee account only as a SHA-256 hash.
3. **A normal transfer.** Pay a small amount to the same beneficiary as before. Each wizard step is
   a checkpoint; the confirmation starts Step Functions, the ledger debits, and the decision is
   archived to the S3 lake (dashed, asynchronous hops).
4. **A takeover.** Ask someone else to type the transfer on your signed-in account, to a new
   beneficiary. Typing and the new payee raise the risk; the friction ceiling holds it at step-up,
   the transfer is held, and the passkey dialog appears. Verify: the map shows Cognito accepting the
   passkey, Step Functions releasing the hold, and `stepup.verified` reaching the adaptation Lambda.
   For a guaranteed takeover profile, run `python simulator/demo_scenarios.py takeover --email <you>`.
5. **A bot.** Tick "Type like a bot" before confirming: perfectly even keystrokes, the automation
   channel fires.
6. **A replay.** Tick "Replay my last confirmed transfer's typing" before confirming: the timing
   matches a remembered session exactly, and the replay detector fires.
7. **A mule.** `python simulator/demo_scenarios.py flagged-payee --account <number>` flags a payee
   as the batch layer would; a transfer to it is restricted and waits for an analyst in the
   operations console (footer link).
8. **The research result.** Close on docs/results.md section 2: naive adaptation is poisonable, a
   static profile rejects genuine drift, and on the deployed configuration the step-up gate is what
   stops poisoning.

Afterwards: `python simulator/demo_scenarios.py clear --email <you> [--account <number>]`.
