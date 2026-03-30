import type { Page } from "@playwright/test";

/** Dismisses the Quick Start footer if it is open (can block catalog search). */
export async function dismissQuickstartIfVisible(page: Page): Promise<void> {
  const quickstartHide = page.getByRole("button", { name: "Hide" });
  if (await quickstartHide.isVisible()) {
    await quickstartHide.click();
    await quickstartHide.waitFor({ state: "hidden", timeout: 5000 });
  }
}
