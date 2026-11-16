"use strict";
const writeBootstrapRevision = require("./write-bootstrap-revision.cjs");
const { buildUniversalMacHelper, buildUniversalSpeechHelper } = require("./build-native-computer.cjs");
let nativeBuild;
let speechBuild;

module.exports = async context => {
  const metadata = writeBootstrapRevision(context);
  if (context.electronPlatformName === "darwin") {
    nativeBuild ||= buildUniversalMacHelper();
    await nativeBuild;
    // An SDK older than macOS 26 cannot build the dictation helper. A local
    // build then ships without it. A release build sets
    // MARIONETTE_REQUIRE_SPEECH_HELPER=1 and fails.
    speechBuild ||= buildUniversalSpeechHelper().catch(error => {
      if (process.env.MARIONETTE_REQUIRE_SPEECH_HELPER === "1") throw error;
      console.warn(`speech-macos helper not built: ${error.message}`);
    });
    await speechBuild;
  }
  return metadata;
};
