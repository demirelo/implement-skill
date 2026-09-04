import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { environment: "node", include: ["red/conformance.case.ts"] },
});
