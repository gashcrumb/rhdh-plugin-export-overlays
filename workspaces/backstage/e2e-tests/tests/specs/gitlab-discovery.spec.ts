import { expect, test } from "@red-hat-developer-hub/e2e-test-utils/test";
import { dismissQuickstartIfVisible } from "../support/hide-quickstart.js";

test.describe("gitlab discovery UI tests", () => {
  test.beforeAll(async ({ rhdh }) => {
    await rhdh.configure({ auth: "guest" });
    await rhdh.deploy();
  });

  test.beforeEach(async ({ loginHelper, uiHelper }) => {
    await loginHelper.loginAsGuest();
    await uiHelper.openSidebar("Catalog");
  });

  test("GitLab integration for discovering catalog entities from GitLab", async ({
    page,
    uiHelper,
  }) => {
    await dismissQuickstartIfVisible(page);

    await page
      .getByRole("textbox", { name: "Search" })
      .waitFor({ state: "visible" });
    await uiHelper.searchInputPlaceholder("scaffoldedForm");
    await uiHelper.verifyTextVisible("scaffoldedForm-test", true, 10_000);
    await uiHelper.clickLink("scaffoldedForm-test");
    await uiHelper.verifyHeading("scaffoldedForm-test");
    await uiHelper.verifyText("My Description");
    await uiHelper.verifyText("experimental");
    await uiHelper.verifyText("website");
    await expect(page.getByRole("link", { name: "View Source" })).toBeVisible();
  });
});
