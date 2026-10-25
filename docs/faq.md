# FAQ

**Does it move real money?** No. v0.1.0 is sandbox only: simulated test USDC
in a local ledger. No real funds, no private keys, no network calls.

**What happens when a payment is outside the policy?** It is rejected with a
reason, unless it exceeds only the approval threshold — then it is queued
for the owner to `approve_payment` or `deny_payment`.

**Can the agent change its own policy?** Policies are granted by the owner
via `set_policy`; the payment path never widens scope.

**How do I stop the agent immediately?** `revoke` — the kill switch freezes
the wallet instantly and timestamps the revocation in the audit log.

**What is the licence/attribution?** A Cyan Eclipse Technologies product;
the "Powered by Cyan Eclipse Technologies" attribution travels with it.
