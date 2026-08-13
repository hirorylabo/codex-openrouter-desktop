// ChatGPT rendererでJavaScriptを評価する最小CDPクライアント。
// 成否ともJSON envelopeを返し、target不在・通信失敗・JS例外は非0終了する。

type Target = {
  type?: string;
  url?: string;
  title?: string;
  webSocketDebuggerUrl?: string;
};

function output(document: unknown, exitCode = 0): never {
  console.log(JSON.stringify(document));
  Deno.exit(exitCode);
}

let targets: Target[];
try {
  const response = await fetch("http://127.0.0.1:9223/json/list");
  if (!response.ok) throw new Error(`target list returned HTTP ${response.status}`);
  targets = await response.json();
} catch (error) {
  output({ ok: false, error: `CDP target list unavailable: ${String(error)}` }, 1);
}

const page = targets.find((target) =>
  target.type === "page" && target.url?.includes("index.html") &&
  !target.url.includes("avatar-overlay") && target.webSocketDebuggerUrl
);
if (!page) {
  output({
    ok: false,
    error: "main page target not found",
    targets: targets.map(({ type, url, title }) => ({ type, url, title })),
  }, 1);
}

try {
  const ws = new WebSocket(page.webSocketDebuggerUrl!);
  let id = 0;
  const pending = new Map<number, (value: any) => void>();
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)!(message);
      pending.delete(message.id);
    }
  };
  await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("CDP WebSocket open timeout")), 10_000);
    ws.onopen = () => {
      clearTimeout(timeout);
      resolve();
    };
    ws.onerror = () => {
      clearTimeout(timeout);
      reject(new Error("CDP WebSocket open failed"));
    };
  });

  function send(method: string, params: any = {}): Promise<any> {
    const requestId = ++id;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error(`${method} timeout`));
      }, 30_000);
      pending.set(requestId, (value) => {
        clearTimeout(timeout);
        resolve(value);
      });
      ws.send(JSON.stringify({ id: requestId, method, params }));
    });
  }

  const evaluated = await send("Runtime.evaluate", {
    expression: Deno.args[0],
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  const exception = evaluated.result?.exceptionDetails;
  if (exception) {
    ws.close();
    output({
      ok: false,
      error: exception.exception?.description ?? exception.text ?? "JavaScript evaluation failed",
      target: { url: page.url, title: page.title },
    }, 1);
  }
  if (evaluated.error) {
    ws.close();
    output({ ok: false, error: evaluated.error.message ?? "CDP evaluation failed" }, 1);
  }
  const value = evaluated.result?.result?.value;
  ws.close();
  output({ ok: true, value: value ?? null });
} catch (error) {
  output({
    ok: false,
    error: String(error),
    target: { url: page.url, title: page.title },
  }, 1);
}
