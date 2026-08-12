// 主ウィンドウのrendererでJSを評価する最小CDPクライアント。
// 使い方: deno run --allow-net=127.0.0.1 cdp.ts '<js expression>'
const list = await (await fetch("http://127.0.0.1:9223/json/list")).json();
const page = list.find((t: any) =>
  t.type === "page" && t.url.includes("index.html") && !t.url.includes("avatar-overlay")
);
if (!page) {
  console.log(JSON.stringify({ error: "main page target not found" }));
  Deno.exit(1);
}
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const pending = new Map<number, (v: any) => void>();
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)!(m);
    pending.delete(m.id);
  }
};
await new Promise((r) => (ws.onopen = () => r(null)));

function send(method: string, params: any = {}): Promise<any> {
  const myId = ++id;
  return new Promise((res) => {
    pending.set(myId, res);
    ws.send(JSON.stringify({ id: myId, method, params }));
  });
}

const expr = Deno.args[0];
const r = await send("Runtime.evaluate", {
  expression: expr,
  returnByValue: true,
  awaitPromise: true,
  userGesture: true,
});
const res = r.result?.result;
if (r.result?.exceptionDetails) {
  console.log(JSON.stringify({
    error: r.result.exceptionDetails.exception?.description ??
      r.result.exceptionDetails.text,
  }));
} else {
  console.log(typeof res?.value === "string"
    ? res.value
    : JSON.stringify(res?.value ?? res));
}
ws.close();
Deno.exit(0);
