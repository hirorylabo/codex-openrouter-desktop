#!/usr/bin/env node
import fs from "node:fs";
import process from "node:process";
import { parse } from "acorn";
import * as walk from "acorn-walk";
import MagicString from "magic-string";

const MARKER = "__codexOpenRouterSemanticCandidateV1";
const VISIBILITY_MARKER = "__codexOpenRouterSemanticVisibilityV1";
const LABEL_MARKER = "__codexOpenRouterSemanticLabelV1";

function fail(message) {
  process.stderr.write(`semantic patcher: ${message}\n`);
  process.exit(1);
}

function parseBundle(source, label) {
  try {
    return parse(source, {
      ecmaVersion: "latest",
      sourceType: "script",
      allowAwaitOutsideFunction: true,
      allowReturnOutsideFunction: true,
    });
  } catch (error) {
    fail(`${label} cannot be parsed: ${error.name}`);
  }
}

function functions(ast) {
  const result = [];
  walk.full(ast, (node) => {
    if (
      node.type === "FunctionDeclaration" ||
      node.type === "FunctionExpression" ||
      node.type === "ArrowFunctionExpression"
    ) {
      result.push(node);
    }
  });
  return result;
}

function propertyName(property) {
  if (property.computed) return null;
  if (property.key.type === "Identifier") return property.key.name;
  if (property.key.type === "Literal") return property.key.value;
  return null;
}

function localName(property) {
  if (property.value?.type === "Identifier") return property.value.name;
  if (property.value?.type === "AssignmentPattern" && property.value.left.type === "Identifier") {
    return property.value.left.name;
  }
  return null;
}

function planRouting(source, ast) {
  const matches = functions(ast).filter((node) => {
    if (node.body?.type !== "BlockStatement" || node.params.length < 2) return false;
    if (node.params[0].type !== "Identifier" || node.params[1].type !== "Identifier") return false;
    const text = source.slice(node.body.start, node.body.end);
    return (
      text.includes("thread/start") &&
      text.includes("config/read") &&
      text.includes("useHostRequestScheduler")
    );
  });
  if (matches.length !== 1) fail(`thread routing anchor matched ${matches.length} functions`);
  const node = matches[0];
  const method = node.params[0].name;
  const params = node.params[1].name;
  const injection =
    `globalThis.__codexDesktopModelProvidersPatchV3=true;globalThis.${MARKER}="routing";` +
    `${method}==="thread/start"&&${params}!=null&&typeof ${params}==="object"&&` +
    `${params}.modelProvider==null&&(${params}={...${params},modelProvider:"openrouter"});` +
    `${method}==="thread/list"&&(${params}=${params}!=null&&typeof ${params}==="object"?` +
    `${params}.modelProviders==null?{...${params},modelProviders:[]}:${params}:{modelProviders:[]});`;
  return { start: node.body.start + 1, end: node.body.start + 1, text: injection };
}

function planVisibility(source, ast) {
  const required = [
    "additionalAvailableModels",
    "authMethod",
    "availableModels",
    "model",
    "useHiddenModels",
  ];
  const matches = [];
  for (const node of functions(ast)) {
    if (node.body?.type !== "BlockStatement" || node.params[0]?.type !== "ObjectPattern") continue;
    const mapping = new Map();
    for (const property of node.params[0].properties) {
      if (property.type !== "Property") continue;
      const key = propertyName(property);
      const value = localName(property);
      if (typeof key === "string" && value) mapping.set(key, value);
    }
    if (required.every((key) => mapping.has(key))) matches.push({ node, mapping });
  }
  if (matches.length !== 1) fail(`model visibility anchor matched ${matches.length} functions`);
  const { node, mapping } = matches[0];
  const additional = mapping.get("additionalAvailableModels");
  const auth = mapping.get("authMethod");
  const available = mapping.get("availableModels");
  const model = mapping.get("model");
  const hidden = mapping.get("useHiddenModels");
  const body = `{globalThis.CodexCustomProviderPickerSection??="${VISIBILITY_MARKER}";return ${additional}?.has(${model}.model)===true||` +
    `(${hidden}&&${auth}==="amazonBedrock"?${available}.has(${model}.model):!${model}.hidden)}`;
  return { start: node.body.start, end: node.body.end, text: body };
}

function planLabelFallback(source, ast) {
  const candidates = [];
  walk.ancestor(ast, {
    Literal(node, ancestors) {
      if (node.value !== "composer.mode.local.model.custom") return;
      for (let index = ancestors.length - 2; index >= 0; index -= 1) {
        const ancestor = ancestors[index];
        if (ancestor.type !== "LogicalExpression" || ancestor.operator !== "??") continue;
        let left = ancestor.left;
        if (left.type === "ChainExpression") left = left.expression;
        if (left.type !== "MemberExpression" || left.computed) continue;
        if (left.property.type !== "Identifier" || left.property.name !== "displayName") continue;
        const objectText = source.slice(left.object.start, left.object.end);
        candidates.push({ node: ancestor.right, objectText });
        return;
      }
    },
  });
  if (candidates.length !== 1) fail(`model label fallback anchor matched ${candidates.length} expressions`);
  const { node, objectText } = candidates[0];
  return {
    start: node.start,
    end: node.end,
    text: `(globalThis.${LABEL_MARKER}??=true,${objectText}?.id??${objectText}?.slug??"Custom")`,
  };
}

function applyPlans(source, plans) {
  const magic = new MagicString(source);
  for (const plan of plans.sort((a, b) => b.start - a.start)) {
    if (plan.start === plan.end) magic.appendLeft(plan.start, plan.text);
    else magic.overwrite(plan.start, plan.end, plan.text);
  }
  return magic.toString();
}

const [, , centralPath, pickerPath] = process.argv;
if (!centralPath || !pickerPath) fail("usage: semantic_transform.mjs CENTRAL PICKER");
const sourceByPath = new Map();
for (const path of new Set([centralPath, pickerPath])) {
  const source = fs.readFileSync(path, "utf8");
  if (source.includes(MARKER)) fail("candidate marker is already present");
  sourceByPath.set(path, source);
}

const centralSource = sourceByPath.get(centralPath);
const pickerSource = sourceByPath.get(pickerPath);
const plansByPath = new Map();
plansByPath.set(centralPath, [planRouting(centralSource, parseBundle(centralSource, "central"))]);
const pickerPlans = [
  planVisibility(pickerSource, parseBundle(pickerSource, "picker")),
  planLabelFallback(pickerSource, parseBundle(pickerSource, "picker")),
];
plansByPath.set(pickerPath, [...(plansByPath.get(pickerPath) ?? []), ...pickerPlans]);

for (const [path, plans] of plansByPath) {
  const patched = applyPlans(sourceByPath.get(path), plans);
  parseBundle(patched, `patched ${path}`);
  fs.writeFileSync(path, patched, "utf8");
}

const packedText = [...new Set([centralPath, pickerPath])]
  .map((path) => fs.readFileSync(path, "utf8"))
  .join("\n");
for (const marker of [MARKER, VISIBILITY_MARKER, LABEL_MARKER]) {
  if ((packedText.match(new RegExp(marker, "g")) ?? []).length !== 1) {
    fail(`${marker} count is not exactly one`);
  }
}
process.stdout.write("semantic candidate transforms: routing=1 visibility=1 label=1\n");
