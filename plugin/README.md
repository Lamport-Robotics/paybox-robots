# lamport-robots plugin

The PayBox side. A TypeScript MCP server bundled into one `dist/plugin.mjs`.

```
pnpm install
pnpm test          # conformance: typecheck, bundle, start, tools/list, sample call
```

Tools: `list_robots`, `robot_status`, `dispatch_task`, `review_payment`,
`pending_payments`, `payment_log`, `fund_robot`, `settle_robot_payment`. The first six
read the hub over HTTPS; the last two return PayBox transaction proposals (USDC on Base,
SOL on Solana, or an x402 paid request) that PayBox simulates and the owner approves.

Runtime network host: `lamport.si`. No variables or secrets.
