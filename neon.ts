import { defineConfig } from "@neon/config/v1";

export default defineConfig({
  // Neon-managed services for the existing Python/Flask Forge application.
  preview: {
    realtime: true,
  },
  auth: true,
  aiGateway: true,
  functions: {
    hello: { name: "Hello World", source: "./hello.ts" },
  },
  branch: (branch) => {
    if (branch.isDefault) return {};
    if (!branch.exists) return { ttl: "7d" };
    return {};
  },
});
