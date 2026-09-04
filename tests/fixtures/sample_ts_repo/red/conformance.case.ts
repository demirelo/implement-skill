import { expect, test } from "vitest";

test("deliberately red conformance probe", () => {
  expect("red").toBe("green");
});
