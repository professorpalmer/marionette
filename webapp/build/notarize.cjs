// electron-builder afterSign hook: notarize the macOS .app when Apple credentials
// are present. Unsigned dev builds still work when env vars are unset.

"use strict";

const { notarize } = require("@electron/notarize");

exports.default = async function notarizeMac(context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== "darwin") return;

  const appleId = process.env.APPLE_ID;
  const appleIdPassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
  const teamId = process.env.APPLE_TEAM_ID;

  if (!appleId || !appleIdPassword || !teamId) {
    console.warn(
      "[notarize] Skipping notarization: set APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, and APPLE_TEAM_ID."
    );
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = `${appOutDir}/${appName}.app`;

  console.log(`[notarize] Notarizing ${appPath}...`);
  await notarize({
    appBundleId: "com.marionette.app",
    appPath,
    appleId,
    appleIdPassword,
    teamId,
  });
  console.log("[notarize] Done.");
};
