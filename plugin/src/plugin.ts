// lamport robots: a paybox plugin that gives robots a wallet.
// the robots talk to the hub at lamport.si; agents reach them here: list, status, budgeted tasks, a read on every
// payment before it moves, and proposals (base usdc, solana sol, x402) that paybox approves and signs. nothing here signs.
import { McpServer } from "@modelcontextprotocol/server";
import { serveStdio } from "@modelcontextprotocol/server/stdio";
import * as z from "zod/v4";
import { encodeFunctionData, erc20Abi, isAddress } from "viem";
import { atomicAmount, evmOutputSchema, proposeEvm, proposeSolana, proposeX402, solanaOutputSchema, x402OutputSchema } from "./paybox.js";

const PROTOCOL_VERSION = "2026-07-28";

// a legacy solana transaction with one system transfer, built by hand: paybox swaps in a fresh blockhash and simulates
const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function base58Decode(s: string): Uint8Array {
  const bytes: number[] = [0];
  for (const ch of s) { let carry = B58.indexOf(ch); if (carry < 0) throw new Error("bad base58"); for (let j = 0; j < bytes.length; j++) { carry += bytes[j]! * 58; bytes[j] = carry & 0xff; carry >>= 8; } while (carry > 0) { bytes.push(carry & 0xff); carry >>= 8; } }
  for (const ch of s) { if (ch !== "1") break; bytes.push(0); }
  const out = Uint8Array.from(bytes.reverse()); if (out.length !== 32) throw new Error("a solana address is 32 bytes"); return out;
}
// the robot's exact amount in base units when it sent one, else usd cents scaled (usdc has 6 decimals: 1 cent = 10_000 units)
const atomicOf = (p: any, perCent: number): string => (p.amount_atomic && /^\d+$/.test(String(p.amount_atomic)) ? String(p.amount_atomic) : String(BigInt(Math.floor(Number(p.amount_cents ?? 0))) * BigInt(perCent)));
function solTransferBase64(from: string, to: string, lamports: bigint): string {
  if (from === to) throw new Error("sender and recipient are the same address");
  const data = new Uint8Array(12); new DataView(data.buffer).setUint32(0, 2, true); new DataView(data.buffer).setBigUint64(4, lamports, true);
  const keys = [base58Decode(from), base58Decode(to), new Uint8Array(32)]; // system program is all zeros
  const msg = [1, 0, 1, 3, ...keys.flatMap((k) => [...k]), ...new Array(32).fill(0), 1, 2, 2, 0, 1, data.length, ...data];
  const tx = Uint8Array.from([1, ...new Array(64).fill(0), ...msg]);
  return Buffer.from(tx).toString("base64");
}
const HUB = "https://lamport.si/api/robots";
const USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";
const ownerCode = z.string().regex(/^[a-z0-9][a-z0-9-]{2,39}$/, "owner codes are 3-40 lowercase letters, digits or dashes");
const robotId = z.string().regex(/^rb_[0-9a-f]{12}$/);
const paymentId = z.string().regex(/^pay_[0-9a-f]{12}$/);
const solanaAddress = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/);
const evmAddress = z.string().regex(/^0x[0-9a-fA-F]{40}$/);
const text = (s: string) => ({ type: "text" as const, text: s });

async function hub(name: string, args: Record<string, unknown>): Promise<any> {
  const r = await fetch(`${HUB}/mcp`, { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }), signal: AbortSignal.timeout(6_000), redirect: "error" });
  if (!r.ok) throw new Error(`hub answered ${r.status}`);
  const j = (await r.json()) as any;
  if (j.error) throw new Error(j.error.message ?? "hub error");
  return j.result?.structuredContent ?? JSON.parse(j.result?.content?.[0]?.text ?? "{}");
}
async function hubGet(path: string): Promise<any> {
  const r = await fetch(`${HUB}${path}`, { headers: { accept: "application/json" }, signal: AbortSignal.timeout(6_000), redirect: "error" });
  if (!r.ok) throw new Error(`hub answered ${r.status}`);
  return r.json();
}
const fail = (msg: string) => ({ isError: true as const, content: [text(msg)] });
const robotSchema = z.object({ id: z.string(), name: z.string(), kind: z.string(), status: z.string().nullable(), battery_pct: z.number().nullable(), budget_cents: z.number().nullable(), spent_cents: z.number().nullable(), wallet: z.string().nullable(), chain: z.string().nullable(), last_seen: z.string().nullable(), online: z.boolean(), meta: z.record(z.string(), z.unknown()) });
const paymentSchema = z.object({ id: z.string(), robot_id: z.string(), robot: z.string().optional(), kind: z.string(), merchant: z.string().nullable(), url: z.string().nullable(), endpoint: z.string().nullable(), asset: z.string().nullable(), amount_cents: z.number().nullable(), purpose: z.string().nullable(), status: z.string(), request_id: z.string().nullable(), review: z.unknown().nullable(), at: z.string() });
const reviewSchema = z.object({ verdict: z.enum(["allow", "hold", "deny"]), risk: z.number(), reasons: z.array(z.string()), facts: z.record(z.string(), z.unknown()), say: z.string() });
const readOnly = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true };
const proposal = { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true };

function createServer() {
  const server = new McpServer({ name: "lamport-robots", title: "PayBox for Robots", version: "0.1.0" }, { supportedProtocolVersions: [PROTOCOL_VERSION] });

  server.registerTool("list_robots", {
    title: "List the owner's robots",
    description: "Robots set up with paybox-robot under the owner's code: name, kind, battery, budget, wallet address and whether they are online now. Ask the owner for their owner_code once; it is the short code they chose when they ran `paybox-robot setup`.",
    inputSchema: z.object({ owner_code: ownerCode }).strict(),
    outputSchema: z.object({ robots: z.array(robotSchema), note: z.string().optional() }).strict(),
    annotations: readOnly,
  }, async ({ owner_code }) => {
    try {
      const out = await hub("list_robots", { owner_code });
      const robots: any[] = out.robots ?? [];
      const lines = robots.map((r) => {
        const budget = ((r.budget_cents ?? 0) / 100).toFixed(2), spent = ((r.spent_cents ?? 0) / 100).toFixed(2);
        return r.name + " (" + r.kind + ") · " + (r.online ? "online" : "offline") + " · battery " + (r.battery_pct ?? "?") + "% · budget $" + budget + " spent $" + spent + " · " + r.id;
      });
      const summary = lines.length ? lines.join("\n") : "No robots under that owner code yet.";
      const structured: Record<string, unknown> = { robots };
      if (out.note) structured.note = out.note;
      return { content: [text(summary)], structuredContent: structured };
    } catch (e) { return fail("The robot hub is unavailable: " + (e as Error).message + ". Check that lamport.si is a configured network host."); }
  });

  server.registerTool("robot_status", {
    title: "One robot in detail",
    description: "Battery, budget, last heartbeat, recent tasks and recent payments of one robot.",
    inputSchema: z.object({ robot_id: robotId }).strict(),
    outputSchema: z.object({ robot: robotSchema, tasks: z.array(z.object({ id: z.string(), task: z.string(), status: z.string(), max_spend_cents: z.number().nullable(), result: z.unknown().nullable() })), payments: z.array(z.object({ id: z.string(), merchant: z.string().nullable(), amount_cents: z.number().nullable(), status: z.string(), purpose: z.string().nullable(), at: z.string() })) }).strict(),
    annotations: readOnly,
  }, async ({ robot_id }) => {
    try { const out = await hub("robot_status", { robot_id }); if (out.error) return fail(out.error); const r = out.robot; return { content: [text(`${r.name}: ${r.online ? "online" : "offline"}, battery ${r.battery_pct ?? "?"}%, budget $${((r.budget_cents ?? 0) / 100).toFixed(2)} with $${(((r.budget_cents ?? 0) - (r.spent_cents ?? 0)) / 100).toFixed(2)} left, ${out.tasks.length} recent tasks, ${out.payments.length} recent payments.`)], structuredContent: out }; }
    catch (e) { return fail(`The robot hub is unavailable: ${(e as Error).message}`); }
  });

  server.registerTool("dispatch_task", {
    title: "Send a robot a task",
    description: "Give a robot a task in plain words with a spending cap in cents. The robot picks it up on its next heartbeat and reports back; anything it needs to pay for comes back as a payment request you settle with settle_robot_payment, so money still passes PayBox approval.",
    inputSchema: z.object({ robot_id: robotId, task: z.string().min(3).max(500), max_spend_cents: z.number().int().min(0).max(1_000_000).optional() }).strict(),
    outputSchema: z.object({ task_id: z.string(), status: z.string(), note: z.string() }).strict(),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
  }, async (input) => {
    try { const out = await hub("dispatch_task", input); if (out.error) return fail(out.error); return { content: [text(`Task ${out.task_id} queued. ${out.note}`)], structuredContent: out }; }
    catch (e) { return fail(`The robot hub is unavailable: ${(e as Error).message}`); }
  });

  server.registerTool("review_payment", {
    title: "Read a recipient before paying",
    description: "Before any payment: what Lamport knows about the recipient. Solana or EVM address age and history, launch-radar wallet labels (sniper, insider, dev), merchant url hygiene and the robot's remaining budget. Returns allow, hold or deny, the reasons, and one line to read aloud to the owner. Run it before settle_robot_payment or fund_robot.",
    inputSchema: z.object({ address: z.string().max(64).optional(), url: z.string().max(2048).optional(), merchant: z.string().max(120).optional(), amount_cents: z.number().int().min(0).optional(), chain: z.string().max(40).optional(), robot_id: robotId.optional() }).strict(),
    outputSchema: reviewSchema.strict(),
    annotations: readOnly,
  }, async (input) => {
    try { const out = await hub("review_payment", input); return { content: [text(`${out.verdict.toUpperCase()} (risk ${out.risk}): ${out.say}`)], structuredContent: out }; }
    catch (e) { return fail(`The review service is unavailable: ${(e as Error).message}`); }
  });

  server.registerTool("pending_payments", {
    title: "Payments the robots are asking for",
    description: "Every payment request the owner's robots have raised and nobody has settled yet, each with Lamport's review attached. Settle one with settle_robot_payment.",
    inputSchema: z.object({ owner_code: ownerCode }).strict(),
    outputSchema: z.object({ payments: z.array(paymentSchema) }).strict(),
    annotations: readOnly,
  }, async ({ owner_code }) => {
    try {
      const out = await hub("pending_payments", { owner_code });
      const ps: any[] = out.payments ?? [];
      const lines = ps.map((p) => p.id + " · " + p.robot + ": " + p.kind + " " + (p.amount || "$" + ((p.amount_cents ?? 0) / 100).toFixed(2)) + " to " + (p.merchant || p.endpoint) + ' for "' + p.purpose + '" · review ' + (p.review?.verdict ?? "?"));
      return { content: [text(lines.length ? lines.join("\n") : "Nothing pending.")], structuredContent: { payments: ps } };
    } catch (e) { return fail("The robot hub is unavailable: " + (e as Error).message); }
  });

  server.registerTool("payment_log", {
    title: "What the robots have paid",
    description: "The owner's robot payments, newest first: requested, proposed, paid or declined, with the review that was done at the time.",
    inputSchema: z.object({ owner_code: ownerCode, limit: z.number().int().min(1).max(50).optional() }).strict(),
    outputSchema: z.object({ payments: z.array(paymentSchema) }).strict(),
    annotations: readOnly,
  }, async (input) => {
    try { const out = await hub("payment_log", input); return { content: [text(`${out.payments.length} payments.`)], structuredContent: out }; }
    catch (e) { return fail(`The robot hub is unavailable: ${(e as Error).message}`); }
  });

  // ---- money: proposals only. paybox shows them, asks the owner, signs and sends.
  server.registerTool("fund_robot", {
    title: "Top up a robot's wallet",
    description: "Propose a transfer from the owner's PayBox wallet to the robot's own wallet: USDC on Base for an EVM robot, SOL on Solana for a Solana robot (pass the owner's Solana address as sender). Returns an unsigned proposal; PayBox approves and signs it. Review first.",
    inputSchema: z.object({ robot_id: robotId, amount: z.string().regex(/^(0|[1-9][0-9]*)(\.[0-9]{1,9})?$/, "a decimal amount such as 2.50"), sender: solanaAddress.optional() }).strict(),
    outputSchema: z.union([evmOutputSchema, solanaOutputSchema]),
    annotations: proposal,
  }, async ({ robot_id, amount, sender }) => {
    let r: any; try { r = (await hub("robot_status", { robot_id })).robot; } catch (e) { return fail(`The robot hub is unavailable: ${(e as Error).message}`); }
    if (!r?.wallet) return fail("This robot has not reported a wallet address yet. Run `paybox-robot setup --wallet <address>` on the robot.");
    try {
      if (isAddress(r.wallet)) {
        const data = encodeFunctionData({ abi: erc20Abi, functionName: "transfer", args: [r.wallet as `0x${string}`, BigInt(atomicAmount(amount, 6))] });
        return proposeEvm({ chain: "eip155:8453", to: USDC_BASE, value: "0", data });
      }
      if (!sender) return fail("For a Solana robot, pass the owner's Solana wallet address as `sender` (PayBox checks it against the wallet chosen for signing).");
      return proposeSolana({ transactionBase64: solTransferBase64(sender, r.wallet, BigInt(atomicAmount(amount, 9))) });
    } catch (e) { return fail(`Could not build the transfer: ${(e as Error).message}`); }
  });

  server.registerTool("settle_robot_payment", {
    title: "Settle a robot's payment request",
    description: "Turn a pending robot payment into a PayBox proposal: an x402 paid request (USDC on Base or Solana) or a transfer to the merchant address (USDC on Base, SOL on Solana with the owner's address as sender). Run review_payment first and read its line to the owner; do not settle a request Lamport marked deny unless the owner insists. After the owner approves, follow the returned request ID to a terminal status and call confirm_robot_payment with it, so the robot learns whether the money actually moved.",
    inputSchema: z.object({ payment_id: paymentId, sender: solanaAddress.optional(), max_amount_cents: z.number().int().min(1).optional() }).strict(),
    outputSchema: z.union([x402OutputSchema, evmOutputSchema, solanaOutputSchema]),
    annotations: proposal,
  }, async ({ payment_id, sender, max_amount_cents }) => {
    let p: any; try { p = await hubGet(`/payments/${payment_id}`); } catch (e) { return fail(`The robot hub is unavailable: ${(e as Error).message}`); }
    if (p.status !== "requested") return fail(`That payment is ${p.status}, not pending.`);
    const cents = max_amount_cents ?? p.amount_cents ?? 0; if (cents <= 0) return fail("The request has no amount.");
    const settle = async (kind: string) => { try { await fetch(`${HUB}/payments/${payment_id}/settle`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ status: "proposed", kind }), signal: AbortSignal.timeout(4_000) }); } catch { /* the log is best effort */ } };
    try {
      if (p.kind === "x402") {
        const chain = p.robot_chain === "solana:mainnet" || solanaAddress.safeParse(p.robot_wallet ?? "").success ? "solana:mainnet" : "eip155:8453";
        const out = proposeX402({ chain, url: String(p.endpoint ?? p.url), maxAmount: String(max_amount_cents ? max_amount_cents * 10_000 : atomicOf(p, 10_000)), method: "GET" });
        await settle("x402"); return out;
      }
      const to = String(p.merchant ?? "");
      if (isAddress(to)) { const data = encodeFunctionData({ abi: erc20Abi, functionName: "transfer", args: [to as `0x${string}`, BigInt(max_amount_cents ? max_amount_cents * 10_000 : atomicOf(p, 10_000))] }); const out = proposeEvm({ chain: "eip155:8453", to: USDC_BASE, value: "0", data }); await settle("transfer"); return out; }
      if (solanaAddress.safeParse(to).success) {
        if (!sender) return fail("Pass the owner's Solana wallet address as `sender`.");
        // the robot quotes solana merchants in lamports (asset SOL, amount_atomic); the hub prices it in usd for the budget
        if (p.asset !== "SOL" || !p.amount_atomic) return fail("Only SOL transfers are supported for Solana merchants in this version; the robot must quote asset SOL with the amount in lamports.");
        if (max_amount_cents && (p.amount_cents ?? 0) > max_amount_cents) return fail("The request (" + p.amount + ") is above your max_amount_cents.");
        const lamports = BigInt(p.amount_atomic);
        const out = proposeSolana({ transactionBase64: solTransferBase64(sender, to, lamports) }); await settle("transfer"); return out;
      }
      return fail("The merchant is not an address and the request is not x402; nothing to propose.");
    } catch (e) { return fail(`Could not build the proposal: ${(e as Error).message}`); }
  });

  server.registerTool("confirm_robot_payment", {
    title: "Confirm what happened to a robot's payment",
    description: "Report the terminal outcome of a settled robot payment back to the robot: after the owner approves and PayBox broadcasts, poll the request ID until success, failed or rejected and pass it here with the transaction hash when there is one. The robot is waiting on this to continue its task. Call it also when the owner declines, so the robot stops waiting.",
    inputSchema: z.object({ payment_id: paymentId, status: z.enum(["success", "failed", "rejected", "pending_confirmation"]), request_id: z.string().max(120).optional(), tx_hash: z.string().max(120).optional(), note: z.string().max(200).optional() }).strict(),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, async ({ payment_id, status, request_id, tx_hash, note }) => {
    try {
      const r = await fetch(`${HUB}/payments/${payment_id}/settle`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ status, request_id, tx_hash, note }), signal: AbortSignal.timeout(8_000) });
      if (!r.ok) return fail("The hub did not accept the confirmation (" + r.status + ").");
      const j = (await r.json()) as any;
      return { content: [text(status === "success" ? "Recorded: " + (j.amount ?? "the payment") + " paid" + (tx_hash ? " (" + tx_hash.slice(0, 10) + "…)" : "") + ". The robot has been told." : "Recorded as " + status + ". The robot will stop waiting.")] };
    } catch (e) { return fail("Could not reach the hub: " + (e instanceof Error ? e.message : String(e))); }
  });

  return server;
}

serveStdio(createServer, { legacy: "reject" });
