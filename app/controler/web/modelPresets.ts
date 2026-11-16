export interface ModelPreset {
  id: string;
  displayName: string;
  efforts: readonly string[];
}

// 2026-11-14 snapshots, not live discovery or account authorization.
// Codex 0.169.0: visible ~/.codex/models_cache.json entries (worker pin remains 0.153.4).
// Amp 0.0.1794672034-g01d6fa: amp plugins show-agent-options --json, capabilities.tools=true.
export const codexEfforts = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
] as const;
const codexModelEfforts = ["low", "medium", "high", "xhigh", "max", "ultra"];
const codexLunaEfforts = ["low", "medium", "high", "xhigh", "max"];
export const codexModels: readonly ModelPreset[] = [
  { id: "gpt-6.1-sol", displayName: "GPT-6.1 Sol", efforts: codexModelEfforts },
  { id: "gpt-6-astra", displayName: "GPT-6 Astra", efforts: codexModelEfforts },
  {
    id: "gpt-6.1-terra",
    displayName: "GPT-6.1 Terra",
    efforts: codexModelEfforts,
  },
  { id: "gpt-6-sol", displayName: "GPT-6 Sol", efforts: codexModelEfforts },
  { id: "gpt-6-luna", displayName: "GPT-6 Luna", efforts: codexLunaEfforts },
  { id: "gpt-5.6-sol", displayName: "GPT-5.6 Sol", efforts: codexModelEfforts },
  {
    id: "gpt-5.6-terra",
    displayName: "GPT-5.6 Terra",
    efforts: codexModelEfforts,
  },
  {
    id: "gpt-5.6-luna",
    displayName: "GPT-5.6 Luna",
    efforts: codexLunaEfforts,
  },
];

export const ampModels: readonly ModelPreset[] = [
  {
    id: "alibaba/qwen3.8-max",
    displayName: "Qwen3.8 Max",
    efforts: ["none", "low", "medium", "xhigh"],
  },
  {
    id: "anthropic/claude-fable-5",
    displayName: "Claude Fable 5",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-fable-5-1",
    displayName: "Claude Fable 5.1",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-fable-5-2",
    displayName: "Claude Fable 5.2",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-haiku-4-5-20251001",
    displayName: "Claude Haiku 4.5",
    efforts: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-haiku-5-5",
    displayName: "Claude Haiku 5.5",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-mythos-5-2",
    displayName: "Claude Mythos 5.2",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-opus-4-6",
    displayName: "Claude Opus 4.6",
    efforts: ["low", "medium", "high", "max"],
  },
  {
    id: "anthropic/claude-opus-4-7",
    displayName: "Claude Opus 4.7",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-opus-4-8",
    displayName: "Claude Opus 4.8",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-opus-5",
    displayName: "Claude Opus 5",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-opus-5-5",
    displayName: "Claude Opus 5.5",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-sonnet-4-5-20250929",
    displayName: "Claude Sonnet 4.5",
    efforts: ["none", "high"],
  },
  {
    id: "anthropic/claude-sonnet-4-6",
    displayName: "Claude Sonnet 4.6",
    efforts: ["none", "high"],
  },
  {
    id: "anthropic/claude-sonnet-5",
    displayName: "Claude Sonnet 5",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "anthropic/claude-sonnet-5-5",
    displayName: "Claude Sonnet 5.5",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "deepseek/deepseek-v4.1-flash",
    displayName: "DeepSeek V4.1 Flash",
    efforts: ["none", "high", "max"],
  },
  {
    id: "google-vertex/gemini-3.1-flash-lite",
    displayName: "Gemini 3.1 Flash Lite",
    efforts: ["minimal", "low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-3.1-pro-preview",
    displayName: "Gemini 3.1 Pro Preview",
    efforts: ["low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-3.5-flash",
    displayName: "Gemini 3.5 Flash",
    efforts: ["minimal", "low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-3.5-flash-lite",
    displayName: "Gemini 3.5 Flash Lite",
    efforts: ["minimal", "low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-3.6-flash",
    displayName: "Gemini 3.6 Flash",
    efforts: ["minimal", "low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-3.8-flash",
    displayName: "Gemini 3.8 Flash",
    efforts: ["low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-4-argon",
    displayName: "Gemini 4 Argon",
    efforts: ["low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-4-carbon",
    displayName: "Gemini 4 Carbon",
    efforts: ["low", "medium", "high"],
  },
  {
    id: "google-vertex/gemini-4-hydrogen",
    displayName: "Gemini 4 Hydrogen",
    efforts: ["low", "medium", "high"],
  },
  {
    id: "meta/muse-spark-1.3",
    displayName: "Muse Spark 1.3",
    efforts: ["minimal", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "meta/muse-wonder-1.0",
    displayName: "Muse Wonder",
    efforts: ["minimal", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "minimax/MiniMax-M3",
    displayName: "MiniMax M3",
    efforts: ["none", "high"],
  },
  {
    id: "moonshotai/kimi-k3",
    displayName: "Kimi K3",
    efforts: ["low", "high", "max"],
  },
  {
    id: "openai/gpt-5",
    displayName: "GPT-5",
    efforts: ["minimal", "low", "medium", "high"],
  },
  {
    id: "openai/gpt-5-mini",
    displayName: "GPT-5 Mini",
    efforts: ["minimal", "low", "medium", "high"],
  },
  {
    id: "openai/gpt-5-nano",
    displayName: "GPT-5 Nano",
    efforts: ["minimal", "low", "medium", "high"],
  },
  {
    id: "openai/gpt-5.1",
    displayName: "GPT-5.1",
    efforts: ["none", "low", "medium", "high"],
  },
  {
    id: "openai/gpt-5.2",
    displayName: "GPT-5.2",
    efforts: ["none", "low", "medium", "high", "xhigh"],
  },
  {
    id: "openai/gpt-5.3-codex",
    displayName: "GPT-5.3 Codex",
    efforts: ["low", "medium", "high", "xhigh"],
  },
  {
    id: "openai/gpt-5.4",
    displayName: "GPT-5.4",
    efforts: ["none", "low", "medium", "high", "xhigh"],
  },
  {
    id: "openai/gpt-5.5",
    displayName: "GPT-5.5",
    efforts: ["none", "low", "medium", "high", "xhigh"],
  },
  {
    id: "openai/gpt-5.6-luna",
    displayName: "GPT-5.6 Luna",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "openai/gpt-5.6-sol",
    displayName: "GPT-5.6 Sol",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "openai/gpt-5.6-terra",
    displayName: "GPT-5.6 Terra",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "openai/gpt-6-astra",
    displayName: "GPT-6 Astra",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "openai/gpt-6-luna",
    displayName: "GPT-6 Luna",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "openai/gpt-6-sol",
    displayName: "GPT-6 Sol",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "openai/gpt-6.1-sol",
    displayName: "GPT-6.1 Sol",
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
  {
    id: "openai/gpt-6.1-terra",
    displayName: "GPT-6.1 Terra",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
  },
  { id: "openai/o3", displayName: "o3", efforts: ["low", "medium", "high"] },
  { id: "xai/grok-4.5", displayName: "Grok 4.5", efforts: [] },
  {
    id: "xai/grok-4.6",
    displayName: "Grok 4.6",
    efforts: ["low", "medium", "high", "xhigh"],
  },
  {
    id: "xai/grok-4.7",
    displayName: "Grok 4.7",
    efforts: ["low", "medium", "high", "xhigh"],
  },
  { id: "xai/grok-build-0.1", displayName: "Grok Build 0.1", efforts: [] },
  {
    id: "zhipuai/glm-5.3",
    displayName: "GLM-5.3",
    efforts: ["low", "high", "max"],
  },
  {
    id: "zhipuai/glm-5.3-flash",
    displayName: "GLM-5.3-Flash",
    efforts: ["low", "high", "max"],
  },
];
