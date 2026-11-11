// Guards the agent debug tab wiring.
//
// The devtools panel receives its fetchers through props, so a missing wire is
// a silent "no data" at runtime. This check reads the real sources and fails
// loudly. Run: node scripts/check-agent-wiring.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
// Paths in the check table are written from the frontend root.
const read = (relative) => readFileSync(resolve(here, "..", relative), "utf8");

const checks = [
  ["模型调用页签", "src/components/DevtoolsPanel.jsx", "'agents', '模型调用'"],
  ["面板接收 fetchAgents", "src/components/DevtoolsPanel.jsx", "fetchAgents,"],
  ["面板接收 fetchAgentCalls", "src/components/DevtoolsPanel.jsx", "fetchAgentCalls,"],
  ["宿主传入 getAgents", "src/components/GameCanvas.jsx", "fetchAgents={getAgents}"],
  ["宿主传入 getAgentCalls", "src/components/GameCanvas.jsx", "fetchAgentCalls={getAgentCalls}"],
  ["API 路由 /agents", "src/api.js", "${V1}/agents"],
  ["API 路由 agent-calls", "src/api.js", "/agent-calls?"],
  ["世界演化页签", "src/components/WorldStudio.jsx", "['evolution', '世界演化']"],
  ["演化面板接入", "src/components/WorldStudio.jsx", "tab === 'evolution'"],
  ["演化面板读提案", "src/components/WorldStudio.jsx", "fetchEvolutionProposals"],
  ["API 路由 evolution/propose", "src/api.js", "/evolution/propose"],
  ["API 路由 evolution/approve", "src/api.js", "/approve"],
];

const forbidden = [
  ["src/agentView.js", "apiKey"],
  ["src/agentView.js", "api_key"],
];

let failed = 0;
for (const [name, file, needle] of checks) {
  if (!read(file).includes(needle)) {
    console.error(`FAIL ${name}: ${file} missing ${JSON.stringify(needle)}`);
    failed += 1;
  }
}
for (const [file, needle] of forbidden) {
  if (read(file).includes(needle)) {
    console.error(`FAIL ${file} must not mention ${needle}`);
    failed += 1;
  }
}
if (failed) process.exit(1);
console.log(`ok: ${checks.length} agent debug wiring checks passed`);
