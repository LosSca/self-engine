// Agent routing in plain language.
//
// The engine can send different work to different models (a narrator, a
// classifier, a memory processor). This module turns the API payload into
// short readable lines so the debug panel never has to guess, and never has
// to show a key: the API does not send one.

const ROLE_LABELS = {
  narrative: "叙事",
  system: "系统",
  memory: "记忆",
};

export function roleLabel(role) {
  return ROLE_LABELS[role] || role || "未知";
}

export function agentsSummary(roles) {
  return (roles || []).map((entry) => {
    let text;
    if (!entry.enabled) {
      text = "已停用";
    } else if (!entry.configured) {
      text = "继承主模型";
    } else {
      text = [entry.provider, entry.model].filter(Boolean).join(" / ");
    }
    return { role: entry.role, label: roleLabel(entry.role), text };
  });
}

export function callLabel(call) {
  const parts = [
    roleLabel(call.role),
    call.task || "调用",
    call.model || "",
    call.status === "error" ? "失败" : "成功",
  ].filter(Boolean);
  if (call.degraded) parts.push("降级");
  if (call.elapsed_ms) parts.push(`${call.elapsed_ms}ms`);
  let text = parts.join(" · ");
  if (call.error) text += ` — ${call.error}`;
  return text;
}
