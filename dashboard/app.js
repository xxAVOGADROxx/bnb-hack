// BSC Wallet Inspector — read-only, keyless, client-side.
// Balances: batch JSON-RPC eth_call(balanceOf) against a public BSC RPC.
// Prices: DexScreener (keyless, CORS-friendly). Token list: tokens.json (public).

const AGENT_WALLET = "0x44dD4C2c353457fF68b164934870BB0391f9251C";
const WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c";
const RPCS = ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org"];
const DEX = "https://api.dexscreener.com/latest/dex/tokens/";
const BAL_SELECTOR = "0x70a08231"; // balanceOf(address)

let TOKENS = [];
let timer = null;

const $ = (id) => document.getElementById(id);
const setStatus = (msg, err = false) => {
  const s = $("status"); s.textContent = msg; s.classList.toggle("err", err);
};

function isAddress(a) { return /^0x[0-9a-fA-F]{40}$/.test(a); }
function pad(addr) { return addr.toLowerCase().replace(/^0x/, "").padStart(64, "0"); }

async function rpc(calls) {
  // calls: [{to, data}] or {method,params}; returns array of hex results.
  const body = calls.map((c, i) => c.method
    ? { jsonrpc: "2.0", id: i, method: c.method, params: c.params }
    : { jsonrpc: "2.0", id: i, method: "eth_call", params: [{ to: c.to, data: c.data }, "latest"] });
  let lastErr;
  for (const url of RPCS) {
    try {
      const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json();
      const out = new Array(calls.length).fill(null);
      for (const r of json) if (r && r.id != null) out[r.id] = r.result;
      return out;
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error("all RPCs failed");
}

async function chunked(arr, n, fn) {
  const out = [];
  for (let i = 0; i < arr.length; i += n) out.push(...await fn(arr.slice(i, i + n)));
  return out;
}

async function getBalances(wallet) {
  // one batched eth_call per token + native BNB
  const calls = TOKENS.map((t) => ({ to: t.address, data: BAL_SELECTOR + pad(wallet) }));
  const results = await chunked(calls, 60, (c) => rpc(c));
  const native = (await rpc([{ method: "eth_getBalance", params: [wallet, "latest"] }]))[0];
  const held = [];
  TOKENS.forEach((t, i) => {
    const raw = results[i] && results[i] !== "0x" ? BigInt(results[i]) : 0n;
    if (raw > 0n) held.push({ ...t, amount: Number(raw) / 10 ** t.decimals });
  });
  const bnb = native ? Number(BigInt(native)) / 1e18 : 0;
  if (bnb > 0) held.push({ symbol: "BNB", address: WBNB, decimals: 18, amount: bnb, native: true });
  return held;
}

async function getPrices(addresses) {
  const prices = {};
  await chunked(addresses, 30, async (batch) => {
    try {
      const res = await fetch(DEX + batch.join(","));
      const json = await res.json();
      for (const p of json.pairs || []) {
        const a = (p.baseToken?.address || "").toLowerCase();
        const liq = p.liquidity?.usd || 0;
        if (!a || !p.priceUsd) continue;
        if (!prices[a] || liq > prices[a].liq) prices[a] = { usd: parseFloat(p.priceUsd), liq };
      }
    } catch (e) { /* leave unpriced */ }
    return [];
  });
  return prices;
}

const fmtUsd = (v) => v >= 1 ? v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  : v.toLocaleString(undefined, { maximumFractionDigits: 6 });
const fmtBal = (v) => v >= 1 ? v.toLocaleString(undefined, { maximumFractionDigits: 4 })
  : v.toLocaleString(undefined, { maximumFractionDigits: 8 });

function render(held, prices) {
  for (const h of held) {
    const p = prices[h.address.toLowerCase()];
    h.price = p ? p.usd : null;
    h.value = h.price != null ? h.amount * h.price : 0;
  }
  held.sort((a, b) => b.value - a.value);
  const total = held.reduce((s, h) => s + h.value, 0);

  const tbody = $("holdings").querySelector("tbody");
  tbody.innerHTML = "";
  for (const h of held) {
    const w = total > 0 ? (h.value / total) * 100 : 0;
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="sym">${h.symbol}${h.native ? " <small>(native)</small>" : ""}</td>` +
      `<td class="num">${fmtBal(h.amount)}</td>` +
      `<td class="num">${h.price != null ? "$" + fmtUsd(h.price) : "—"}</td>` +
      `<td class="num">${h.value ? "$" + fmtUsd(h.value) : "—"}</td>` +
      `<td class="num">${w.toFixed(1)}%<span class="bar" style="width:${Math.max(2, w * 0.6)}px"></span></td>`;
    tbody.appendChild(tr);
  }
  $("total").textContent = "$" + fmtUsd(total);
  $("count").textContent = `${held.length} holding${held.length === 1 ? "" : "s"}`;
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  $("summary").classList.remove("hidden");
  $("holdings").classList.remove("hidden");
}

async function inspect() {
  const wallet = $("wallet").value.trim();
  if (!isAddress(wallet)) return setStatus("enter a valid 0x… BSC address", true);
  $("scan").href = "https://bscscan.com/address/" + wallet;
  $("go").disabled = true;
  setStatus("reading on-chain balances…");
  try {
    const held = await getBalances(wallet);
    if (!held.length) { setStatus("no eligible-token holdings found for this wallet"); }
    else {
      setStatus("fetching prices…");
      const prices = await getPrices([...new Set(held.map((h) => h.address.toLowerCase()))]);
      render(held, prices);
      setStatus("");
    }
  } catch (e) {
    setStatus("error: " + (e.message || e), true);
  } finally {
    $("go").disabled = false;
  }
}

async function init() {
  $("wallet").value = AGENT_WALLET;
  try {
    TOKENS = await (await fetch("tokens.json")).json();
  } catch (e) { return setStatus("could not load token list", true); }
  $("go").addEventListener("click", inspect);
  $("wallet").addEventListener("keydown", (e) => { if (e.key === "Enter") inspect(); });
  $("auto").addEventListener("change", (e) => {
    clearInterval(timer);
    if (e.target.checked) timer = setInterval(inspect, 30000);
  });
  inspect();
}

init();
