# Demo walkthrough

About ten minutes, on the deployed dev stack. Open the client URL from `terraform output client_url`
on a laptop with a passkey-capable browser, and keep **Under the hood** open on the right: it shows
every request's route through AWS, its measured latency, the decision, and the exact payload sent.

## Before the demo

1. Sign in once with your test account and add a passkey (Security, Add a passkey).
2. Give the account two months of ordinary use, through the real pipeline:
   `python simulator/demo_scenarios.py history --email <you>`. It writes released transfers to four
   regular payees into the ledger and the audit lake, runs the deployed aggregator over them, and
   enrols the laptop's browser. Paste the snippet it prints into the browser console, so the
   statement names the payees. Re-run it on the day if the demo is not today: it replaces itself.
3. Make yourself an analyst, to release held transfers in the operations console:
   `python simulator/demo_scenarios.py analyst --email <you>`.
4. Make one small transfer to a regular payee, so there is a confirmed session to replay later.

## The story

1. **Sign in.** The map lights Cognito, then API Gateway, λ scoring and DynamoDB for the login
   check. Point at the waterfall: the Lambda itself takes about 25 ms; the rest is API Gateway and
   the network.
2. **What leaves the browser.** Open the payload panel: timing arrays and three counts, no
   characters, no key codes, and the payee account only as a SHA-256 hash.
3. **A normal transfer.** Pay a small amount to a regular payee (Maa, account 302118765432). Each wizard step is
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
7. **An unusual amount.** Pay ₹2,00,000 to a new account. Against two months of payments around
   ₹1,000, the transaction channel alerts on its own: the transfer is restricted and held, and an
   analyst releases it from the operations console. The ledger debits only then.
8. **A mule.** `python simulator/demo_scenarios.py flagged-payee --account <number>` flags a payee
   as the batch layer would; a transfer to it is restricted and waits for an analyst in the
   operations console (footer link).
9. **The research result.** Close on docs/results.md section 2: naive adaptation is poisonable, a
   static profile rejects genuine drift, and on the deployed configuration the step-up gate is what
   stops poisoning.

Afterwards: `python simulator/demo_scenarios.py clear --email <you> [--account <number>]`. It also
removes the seeded history from the lake and the ledger and re-runs the aggregator.
