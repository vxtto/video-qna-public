import { test, expect } from "@playwright/test";

// Two tests only, on purpose (see repo conversation this suite was scoped
// from): a real browser is the only way to test this plain-JS,
// no-build-step frontend at all, but it's also the slowest/flakiest layer
// available - so it's kept to exactly the two things that actually need a
// live <video> element and real DOM events: the transcript click-to-seek
// wiring, and the chat-citation click-to-seek wiring. Everything about
// answer *quality* is out of scope here.
//
// Requires the app stack running with the fixture video seeded - see
// e2e/README.md for how CI/local runs bring that up first.

async function waitForVideoLoaded(page: import("@playwright/test").Page) {
  await page.waitForFunction(() => {
    const el = document.querySelector("video#player") as HTMLVideoElement | null;
    return !!el && el.duration > 0;
  });
}

async function waitForCurrentTimeToChange(page: import("@playwright/test").Page, previous: number) {
  await page.waitForFunction((prev) => {
    const el = document.querySelector("video#player") as HTMLVideoElement | null;
    return !!el && Math.abs(el.currentTime - prev) > 0.1;
  }, previous);
}

test("video loads and a mid-transcript click seeks + plays it", async ({ page }) => {
  await page.goto("/");
  await waitForVideoLoaded(page);

  const player = page.locator("#player");
  const segments = page.locator("#segments li");
  await expect(segments).toHaveCount(3);

  const before = await player.evaluate((el: HTMLVideoElement) => el.currentTime);
  await segments.nth(1).click(); // the fixture's middle segment, start_ms = 2000

  await waitForCurrentTimeToChange(page, before);
  const currentTime = await player.evaluate((el: HTMLVideoElement) => el.currentTime);
  expect(currentTime).toBeGreaterThan(1.5);
  expect(currentTime).toBeLessThan(2.5);

  await expect
    .poll(() => player.evaluate((el: HTMLVideoElement) => el.paused))
    .toBe(false);
});

test("clicking a chat citation timestamp seeks the player", async ({ page }) => {
  await page.goto("/");
  await waitForVideoLoaded(page);

  const videos: Array<{ id: number }> = await page.evaluate(() =>
    fetch("/api/videos").then((r) => r.json())
  );
  const videoId = videos[0].id;

  // Mock the chat backend with a canned SSE response - this test is about
  // the frontend's click-to-seek wiring, not agent/LLM quality so no real OpenRouter call happens
  // and the result is fully deterministic.
  await page.route("**/api/chat/stream", async (route) => {
    const body =
      'event: session\ndata: {"session_id": "00000000-0000-0000-0000-000000000000"}\n\n' +
      "event: final\n" +
      `data: {"message_id": 1, "answer": "canned answer", "citations": ` +
      `[{"chunk_id": 1, "video_id": ${videoId}, "start_ms": 4000}], "trace": []}\n\n`;
    await route.fulfill({ status: 200, contentType: "text/event-stream", body });
  });

  await page.fill("#chat-input", "what happens at the end?");
  await page.click("#chat-form button[type=submit]");

  const citationButton = page.locator(".citations button").first();
  await expect(citationButton).toBeVisible();

  const player = page.locator("#player");
  const before = await player.evaluate((el: HTMLVideoElement) => el.currentTime);
  await citationButton.click();

  await waitForCurrentTimeToChange(page, before);
  const currentTime = await player.evaluate((el: HTMLVideoElement) => el.currentTime);
  expect(currentTime).toBeGreaterThan(3.5);
  expect(currentTime).toBeLessThan(4.5);
});
