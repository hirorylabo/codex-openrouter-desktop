import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const transform = path.resolve("semantic_transform.mjs");

function validSource(extraRouting = "") {
  return `
function route(method, params) { if (method === "thread/start") {} if (method === "config/read") {} this.useHostRequestScheduler; }
${extraRouting}
function visible({additionalAvailableModels:a,authMethod:b,availableModels:c,model:d,useHiddenModels:e}) { return false; }
const label = item?.displayName ?? format({id:"composer.mode.local.model.custom"});
`;
}

function moduleSource(extraRouting = "") {
  return `
import "./runtime.js";
class Client {
  enqueueRequest(method, params, options, dispatch = () => {}, trace = null) {
    if (!this.useHostRequestScheduler) log("mcp_request_queue_full");
    this.queuedRequests.push({method, params, options, dispatch, trace});
  }
}
${extraRouting}
function visible({additionalAvailableModels:a,authMethod:b,availableModels:c,model:d,useHiddenModels:e}) { return false; }
const label = item?.displayName ?? format({id:\`composer.mode.local.model.custom\`});
`;
}

function execute(source) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "semantic-transform-"));
  const bundle = path.join(directory, "bundle.js");
  fs.writeFileSync(bundle, source);
  const result = spawnSync(process.execPath, [transform, bundle, bundle], {encoding:"utf8"});
  return {result, output: fs.readFileSync(bundle, "utf8")};
}

test("exactly one semantic anchor set is transformed", () => {
  const {result, output} = execute(validSource());
  assert.equal(result.status, 0, result.stderr);
  assert.match(output, /__codexOpenRouterSemanticCandidateV1/);
  assert.match(output, /__codexOpenRouterSemanticVisibilityV1/);
  assert.match(output, /__codexOpenRouterSemanticLabelV1/);
});

test("ES module queue routing and template label are transformed", () => {
  const {result, output} = execute(moduleSource());
  assert.equal(result.status, 0, result.stderr);
  assert.match(output, /__codexOpenRouterSemanticCandidateV1/);
  assert.match(output, /__codexOpenRouterSemanticVisibilityV1/);
  assert.match(output, /__codexOpenRouterSemanticLabelV1/);
});

test("zero matching anchor fails closed", () => {
  const {result} = execute("const broken = true;");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /matched 0/);
});

test("multiple routing anchors fail closed", () => {
  const duplicate = 'function route2(m,p){if(m==="thread/start"){} if(m==="config/read"){} this.useHostRequestScheduler;}';
  const {result} = execute(validSource(duplicate));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /matched 2/);
});

test("multiple queue routing anchors fail closed", () => {
  const duplicate =
    'function route2(m,p){this.useHostRequestScheduler; log("mcp_request_queue_full"); this.queuedRequests.push({m,p});}';
  const {result} = execute(moduleSource(duplicate));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /matched 2/);
});

test("syntactically corrupt bundle fails closed", () => {
  const {result} = execute("function {");
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot be parsed/);
});
