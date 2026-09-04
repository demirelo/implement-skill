import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "node", include: ["sum.test.ts"] },
});
