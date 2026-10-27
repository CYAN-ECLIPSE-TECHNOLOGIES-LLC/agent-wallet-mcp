"""Chain-family adapters for Agent Wallet B0 (LOCAL/SANDBOX ONLY).

Only two adapters exist in B0: SandboxAdapter and EvmAdapter (the latter
operates against the in-memory sandbox ledger only - no RPC, no network,
no chain transactions). There is deliberately NO bitcoin module and no
BTC payment path: BTC is a reserve-role asset under invariant R/P-1.
"""
